
from typing import Dict, Optional, List
import logging

from fastapi import FastAPI

from ray import serve


from pydantic import BaseModel

class ServeArgs(BaseModel):
    models: str

logger = logging.getLogger("ray.serve")

app = FastAPI()


@serve.deployment(
    autoscaling_config={
        "min_replicas": 1,
        "max_replicas": 10,
        "target_ongoing_requests": 5,
    },
    max_ongoing_requests=10,
)

@serve.ingress(app)
class VLLMDeployment:
    def __init__(
        self,
        # args: ServeArgs,
    ):
        # logger.info(f"Starting with engine args: {args}")
        pass
    
    async def reconfigure(
        self,
        config: any,
        # config: Union[dict, Args],
    ) -> None:
        logger.info(f"--------: {config}")
       
def build_app(cli_args: Dict[str, str]) -> serve.Application:
    """Builds the Serve app based on CLI arguments.

    See https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html#command-line-arguments-for-the-server
    for the complete set of arguments.

    Supported engine arguments: https://docs.vllm.ai/en/latest/models/engine_args.html.
    """  # noqa: E501
    # parsed_args = parse_vllm_args(cli_args)
    # engine_args = AsyncEngineArgs.from_cli_args(parsed_args)
    # engine_args.worker_use_ray = True

    # tp = engine_args.tensor_parallel_size
    tp = 2
    logger.info(f"Tensor parallelism = {tp}")
    pg_resources = []
    pg_resources.append({"CPU": 1})  # for the deployment replica
    for i in range(tp):
        pg_resources.append({"CPU": 1, "GPU": 1})  # for the vLLM actors

    # We use the "STRICT_PACK" strategy below to ensure all vLLM actors are placed on
    # the same Ray node.

    serve_args = ServeArgs(models="test")
    return VLLMDeployment.options(
        placement_group_bundles=pg_resources, placement_group_strategy="STRICT_PACK", user_config=serve_args
    ).bind()