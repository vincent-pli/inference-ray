
from ._base import LLMEngine
import asyncio
from typing import List, Optional, Any
from ray.air import ScalingConfig
from ray.util.placement_group import PlacementGroup
from llmserve.backend.server.models import Args, LLMConfig, Prompt, Response
from llmserve.backend.logger import get_logger
from .vllm_compatibility import AsyncLLMEngineRay

logger = get_logger(__name__)
class VllmEngine(LLMEngine):
    _engine_cls = AsyncLLMEngineRay

    def __init__(
        self,
        args: Args,

    ):
        if not (args.scaling_config.num_gpus_per_worker > 0):
            raise ValueError("The VLLM Engine Requires > 0 GPUs to run.")
        self.running = False
        
        super().__init__(args = args)

    async def launch_engine(
        self, 
        scaling_config: ScalingConfig,
        placement_group: PlacementGroup,
        scaling_options: dict,
    ) -> Any:
        if self.running:
            # The engine is already running!
            logger.info("Skipping engine restart because the engine is already running")
            return

        config: Args = self.args  # pylint:disable=no-member
        llm_config = config.model_conf
        runtime_env = llm_config.initialization.runtime_env or {}

        self.engine = self._engine_cls.from_llm_app(
                self.args,
                scaling_options,
                placement_group,
                runtime_env,
            )
        self.running = True

    async def predict(
            self,
            prompts: List[Prompt],
            *,
            timeout_s: float = 60,
            start_timestamp: Optional[float] = None,
            lock: asyncio.Lock,
        ) -> List[str]:
        """Load model.

        Args:
            model_id (str): Hugging Face model ID.
        """
        pass