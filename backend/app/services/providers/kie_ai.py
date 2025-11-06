# backend/app/services/providers/kie_ai.py
from app.services.providers.base import Provider
from typing import Mapping

class KieAIProvider(Provider):
    """Kie AI 提供商，提供多种模型"""

    def key(self) -> str:
        return "kie-ai"

    def display_name(self) -> str:
        return "Kie AI"

    def capabilities(self) -> Mapping[str, object]:
        # 这里可以返回不同模型的任务处理能力
        return {
            "models": ["sora-2-image-to-video", "model-2", "model-3"],  # 模型列表
            "tasks": ["createTask", "recordInfo"]  # 任务类型
        }

    def create_task(self, model: str, input_data: dict):
        """根据模型创建任务"""
        if model == "sora-2-image-to-video":
            return self.create_sora_task(input_data)
        # 可以扩展其他模型的任务创建
        return None

    def create_sora_task(self, input_data: dict):
        """处理 sora-2-image-to-video 任务"""
        # 这里调用 Kie AI 的 API 创建任务，任务处理的具体逻辑
        pass

    def query_task(self, task_id: str):
        """查询任务状态"""
        # 查询任务状态的实现
        pass

