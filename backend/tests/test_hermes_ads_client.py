from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.core.errors import APIError
from app.services.hermes_agent.client import (
    HermesAdsRealtimeClient,
    HermesAdsReviewClient,
    HermesAgentClient,
    HermesContentCriticClient,
    HermesContentDirectorClient,
    HermesContentProducerClient,
)


class HermesAdsClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_ads_endpoint_failure_does_not_fall_back_to_primary(self) -> None:
        with patch.object(HermesAgentClient, "create_response", new_callable=AsyncMock) as create_response:
            create_response.side_effect = APIError("HERMES_NETWORK_ERROR", "unreachable", 502)
            client = HermesAdsRealtimeClient()
            with self.assertRaises(APIError):
                await client.create_response(input_text="{}", instructions="test")

        self.assertEqual(1, create_response.await_count)

    async def test_non_transient_error_is_not_retried_on_primary(self) -> None:
        with patch.object(HermesAgentClient, "create_response", new_callable=AsyncMock) as create_response:
            create_response.side_effect = APIError("HERMES_POLICY_ERROR", "blocked", 400)
            client = HermesAdsRealtimeClient()
            with self.assertRaises(APIError):
                await client.create_response(input_text="{}", instructions="test")

        self.assertEqual(1, create_response.await_count)

    def test_realtime_and_review_use_distinct_endpoints(self) -> None:
        realtime = HermesAdsRealtimeClient()
        review = HermesAdsReviewClient()

        self.assertNotEqual(realtime.base_url, review.base_url)
        self.assertEqual("ads_realtime", realtime.role)
        self.assertEqual("ads_review", review.role)

    async def test_ads_requests_are_not_stored_and_include_role_metadata(self) -> None:
        with patch.object(HermesAgentClient, "create_response", new_callable=AsyncMock) as create_response:
            create_response.return_value = ({"output_text": "ok"}, 5)
            client = HermesAdsReviewClient()
            await client.create_response(input_text="{}", instructions="test", metadata={"source": "unit"})

        kwargs = create_response.await_args.kwargs
        self.assertFalse(kwargs["store"])
        self.assertEqual("ads_review", kwargs["metadata"]["agent_role"])
        self.assertEqual("unit", kwargs["metadata"]["source"])
        self.assertTrue(kwargs["metadata"]["request_id"])


class HermesContentClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_request_has_a_total_wall_clock_timeout(self) -> None:
        async def _never_returns(*_args, **_kwargs):
            await asyncio.sleep(60)

        client = HermesAgentClient(
            base_url="http://127.0.0.1:1/v1",
            api_key="unit-test",
            model="unit-test",
            timeout=0.01,
            enabled=True,
        )
        with patch.object(
            httpx.AsyncClient,
            "post",
            side_effect=_never_returns,
        ):
            with self.assertRaises(APIError) as raised:
                await client.create_response(
                    input_text="{}",
                    instructions="test",
                )

        self.assertEqual("HERMES_TIMEOUT", raised.exception.code)

    def test_director_and_critic_use_separate_isolated_content_endpoints(self) -> None:
        director = HermesContentDirectorClient()
        critic = HermesContentCriticClient()
        producer = HermesContentProducerClient()

        self.assertNotEqual(director.base_url, critic.base_url)
        self.assertNotEqual(producer.base_url, director.base_url)
        self.assertNotEqual(producer.base_url, critic.base_url)
        self.assertNotEqual(director.base_url, HermesAgentClient().base_url)
        self.assertNotEqual(director.base_url, HermesAdsRealtimeClient().base_url)
        self.assertNotEqual(director.base_url, HermesAdsReviewClient().base_url)
        self.assertNotEqual(critic.base_url, HermesAgentClient().base_url)
        self.assertNotEqual(critic.base_url, HermesAdsRealtimeClient().base_url)
        self.assertNotEqual(critic.base_url, HermesAdsReviewClient().base_url)
        self.assertEqual("content_director", director.role)
        self.assertEqual("content_critic", critic.role)
        self.assertEqual("content_producer", producer.role)

    async def test_producer_keeps_only_its_scoped_conversation_and_role_metadata(self) -> None:
        with patch.object(HermesAgentClient, "create_response", new_callable=AsyncMock) as create_response:
            create_response.return_value = ({"output_text": "ok"}, 5)
            client = HermesContentProducerClient()
            await client.create_response(
                input_text="{}",
                instructions="test",
                conversation="gmv-cf-producer-scoped",
                session_key="gmv-cf-producer-scoped",
                metadata={"prompt_version": "content_producer_v2"},
            )

        kwargs = create_response.await_args.kwargs
        self.assertTrue(kwargs["store"])
        self.assertEqual("gmv-cf-producer-scoped", kwargs["conversation"])
        self.assertEqual("gmv-cf-producer-scoped", kwargs["session_key"])
        self.assertEqual("content_producer", kwargs["metadata"]["agent_role"])
        self.assertEqual(kwargs["idempotency_key"], kwargs["metadata"]["request_id"])

    async def test_content_roles_are_stateless_and_include_role_metadata(self) -> None:
        with patch.object(HermesAgentClient, "create_response", new_callable=AsyncMock) as create_response:
            create_response.return_value = ({"output_text": "ok"}, 5)
            client = HermesContentCriticClient()
            await client.create_response(
                input_text="{}",
                instructions="test",
                metadata={"artifact_sha256": "abc"},
            )

        kwargs = create_response.await_args.kwargs
        self.assertFalse(kwargs["store"])
        self.assertIsNone(kwargs["conversation"])
        self.assertIsNone(kwargs["previous_response_id"])
        self.assertEqual("content_critic", kwargs["metadata"]["agent_role"])
        self.assertEqual("abc", kwargs["metadata"]["artifact_sha256"])
        self.assertTrue(kwargs["metadata"]["request_id"])
        self.assertEqual(kwargs["idempotency_key"], kwargs["metadata"]["request_id"])

    async def test_content_roles_reject_hidden_conversation_context(self) -> None:
        client = HermesContentDirectorClient()
        with self.assertRaisesRegex(APIError, "explicit stateless input packet"):
            await client.create_response(
                input_text="{}",
                instructions="test",
                conversation="author-history",
            )

    async def test_content_endpoint_failure_does_not_fall_back(self) -> None:
        with patch.object(HermesAgentClient, "create_response", new_callable=AsyncMock) as create_response:
            create_response.side_effect = APIError("HERMES_NETWORK_ERROR", "unreachable", 502)
            client = HermesContentDirectorClient()
            with self.assertRaises(APIError):
                await client.create_response(input_text="{}", instructions="test")

        self.assertEqual(1, create_response.await_count)

    async def test_content_idempotency_key_is_stable_per_role_and_artifact(self) -> None:
        with patch.object(HermesAgentClient, "create_response", new_callable=AsyncMock) as create_response:
            create_response.return_value = ({"output_text": "ok"}, 5)
            client = HermesContentDirectorClient()
            await client.create_response(input_text='{"revision":1}', instructions="author")
            first = create_response.await_args.kwargs["idempotency_key"]
            await client.create_response(input_text='{"revision":1}', instructions="author")
            second = create_response.await_args.kwargs["idempotency_key"]
            await client.create_response(input_text='{"revision":2}', instructions="author")
            third = create_response.await_args.kwargs["idempotency_key"]

        self.assertEqual(first, second)
        self.assertNotEqual(first, third)
        self.assertTrue(first.startswith("gmv-content-content_director-"))
