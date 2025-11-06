# tests/test_sora2.py
from __future__ import annotations

import asyncio
import os

from app.services.kie_api.sora2 import sora, KieApiError


KIE_API_KEY = os.environ.get("KIE_API_KEY", "").strip()


async def main() -> None:
    if not KIE_API_KEY:
        raise RuntimeError("请先在环境变量 KIE_API_KEY 中设置你的 Kie API Key")

    try:
        create_resp = await sora.create_image_to_video_task(
            api_key=KIE_API_KEY,
            model="sora-2-image-to-video",
            input={
                "prompt": (
                    "A claymation conductor passionately leads a claymation orchestra, "
                    "while the entire group joyfully sings in chorus the phrase: "
                    "\"Sora 2 is now available on Kie AI.\""
                ),
                "image_urls": [
                    "https://file.aiquickdraw.com/custom-page/akr/section-images/17594315607644506ltpf.jpg"
                ],
                "aspect_ratio": "landscape",
                "n_frames": "10",
                "remove_watermark": True,
            },
            # 如果你有自己的回调地址，可以在这里填：
            # callback_url="https://your-domain.com/api/callback",
        )
    except KieApiError as e:
        print("createTask 业务错误:", e.code, e.message)
        print("原始返回:", e.raw)
        return

    print("createTask 返回:", create_resp)

    task_id = None
    if isinstance(create_resp, dict):
        data = create_resp.get("data") or {}
        task_id = data.get("taskId") or data.get("task_id")

    if not task_id:
        print("未拿到 taskId，无法继续查询 recordInfo")
        return

    print("taskId =", task_id)

    try:
        record = await sora.get_task_record(api_key=KIE_API_KEY, task_id=task_id)
    except KieApiError as e:
        print("recordInfo 业务错误:", e.code, e.message)
        print("原始返回:", e.raw)
        return

    print("recordInfo 返回:", record)


if __name__ == "__main__":
    asyncio.run(main())

