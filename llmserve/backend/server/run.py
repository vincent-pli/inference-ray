import sys
from typing import Any, List, Union, Optional, Dict
import datetime
import ray._private.usage.usage_lib
from ray import serve
import json

from llmserve.backend.server.app import LLMDeployment, RouterDeployment, ExperimentalDeployment, VLLMDeployment
from llmserve.backend.server.apiserver import ApiServer
# from llmserve.backend.server.app import ApplicationDeployment
from llmserve.backend.server.config import SERVE_RUN_HOST
from llmserve.backend.server.models import LLMApp, ServeArgs
from llmserve.backend.server.utils import parse_args, get_serve_port
import uuid
import os
from llmserve.backend.logger import get_logger
from ray.serve._private.constants import (DEFAULT_HTTP_PORT)
from llmserve.common.utils import _replace_prefix, _reverse_prefix

# ray.init(address="auto")
logger = get_logger(__name__)

def get_serve_start_port(port: int):
    serve_start_port = port
    serve_runtime_port = get_serve_port()
    if serve_runtime_port > -1:
        logger.info(
            f"Serve is already running at {SERVE_RUN_HOST}:{serve_runtime_port}")
        serve_start_port = serve_runtime_port
    return serve_start_port


def llm_server(args: Union[str, LLMApp, List[Union[LLMApp, str]]]):
    # tp = 2
    # logger.info(f"Tensor parallelism = {tp}")
    # pg_resources = []
    # pg_resources.append({"CPU": 1})  # for the deployment replica
    # for i in range(tp):
    #     pg_resources.append({"CPU": 1, "GPU": 1})  # for the vLLM actors

    # return VLLMDeployment.options(
    #     placement_group_bundles=pg_resources, 
    #     placement_group_strategy="STRICT_PACK",
    #     # name=_reverse_prefix(model.model_conf.model_id),
    #     # max_ongoing_requests=max_ongoing_requests,
    #     # user_config=user_config,
    #     # **deployment_config,
    # ).bind()
    # logger.info("------------")
    # logger.info(pg_resources)
    # return deployments[model.model_conf.model_id]
    """Serve LLM Models

    This function returns a Ray Serve Application.

    Accepted inputs:
    1. The path to a yaml file defining your LLMApp
    2. The path to a folder containing yaml files, which define your LLMApps
    2. A list of yaml files defining multiple LLMApps
    3. A dict or LLMApp object
    4. A list of dicts or LLMApp objects

    You can use `serve.run` to run this application on the local Ray Cluster.

    `serve.run(llm_backend(args))`.

    You can also remove
    """
    models = parse_args(args)
    if not models:
        raise RuntimeError("No enabled models were found.")

    # For each model, create a deployment
    # deployments = {}
    # model_configs = {}
    from ray.serve.handle import DeploymentHandle
    deployment: DeploymentHandle
    model = models[0]
    # for model in models:
    # if model.model_conf.model_id in model_configs:
    #     raise ValueError(
    #         f"Duplicate model_id {model.model_conf.model_id} specified. "
    #         "Please ensure that each model has a unique model_id. "
    #         "If you want two models to share the same Hugging Face Hub ID, "
    #         "specify initialization.hf_model_id in the model config."
    #     )
    logger.info(f"Initializing LLM app {model.model_dump_json(indent=2)}")
    # user_config is one of the parameter for ray.deployment.option(), it will trigger reconfigure() in the ray deplyment 
    # and set user_config as it's param
    user_config = model.model_dump()
    deployment_config = model.deployment_config.model_dump()
    # model_configs[model.model_conf.model_id] = model
    deployment_config = deployment_config.copy()

    # if user_config.get("model_config", {}).get("initialization", {}).get("initializer", {}).get("type", None) == "Vllm" and user_config.get("model_config", {}).get("initialization", {}).get("runtime_env", None):
    #     deployment_config["ray_actor_options"]["runtime_env"] = user_config.get("model_config", {}).get("initialization", {}).get("runtime_env", None)
    # max_ongoing_requests = deployment_config.pop(
    #     "max_ongoing_requests", None
    # ) or user_config.get("model_conf", {}).get("generation", {}).get("max_batch_size", 1)
    import math
    max_ongoing_requests = math.ceil(model.deployment_config.autoscaling_config.target_ongoing_requests * 1.2)
    # deployment_config.pop("ray_actor_options")
    # tp = model.scaling_config.num_workers
    # logger.info(f"Tensor parallelism = {tp}")
    # pg_resources = []
    # pg_resources.append({"CPU": 1})  # for the deployment replica
    # for i in range(tp):
    #     pg_resources.append({"CPU": 1, "GPU": 1})  # for the vLLM actors
    if model.model_conf.initialization.initializer.type == "Vllm":
        deployment_config.pop("ray_actor_options")
        tp = model.scaling_config.num_workers
        logger.info(f"Tensor parallelism = {tp}")
        pg_resources = []
        pg_resources.append({"CPU": 1})  # for the deployment replica
        for i in range(tp):
            pg_resources.append({"CPU": 1, "GPU": 1})  # for the vLLM actors

        deployment = VLLMDeployment.options(
            placement_group_bundles=pg_resources, 
            placement_group_strategy="STRICT_PACK",
            name=_reverse_prefix(model.model_conf.model_id),
            max_ongoing_requests=max_ongoing_requests,
            user_config=user_config,
            **deployment_config,
        ).bind()
        # return deployments[model.model_conf.model_id]
    else:
        deployment = LLMDeployment.options(  # pylint:disable=no-member
            name=_reverse_prefix(model.model_conf.model_id),
            max_ongoing_requests=max_ongoing_requests,
            user_config=user_config,
            **deployment_config,
        ).bind()

    return deployment
    return RouterDeployment.options(
        name=('+'.join([_reverse_prefix(model.model_conf.model_id) for model in models])) + "-router",
    ).bind(deployments, model_configs)  # pylint:disable=no-member


def llm_experimental(args: Union[str, LLMApp, List[Union[LLMApp, str]]]):
    """Serve LLM Models

    This function returns a Ray Serve Application.

    Accepted inputs:
    1. The path to a yaml file defining your LLMApp
    2. The path to a folder containing yaml files, which define your LLMApps
    2. A list of yaml files defining multiple LLMApps
    3. A dict or LLMApp object
    4. A list of dicts or LLMApp objects

    You can use `serve.run` to run this application on the local Ray Cluster.

    `serve.run(llm_backend(args))`.

    You can also remove
    """
    models = parse_args(args)
    if not models:
        raise RuntimeError("No enabled models were found.")

    # For model, create a deployment
    model = models[0]

    if isinstance(model, LLMApp):
        logger.info(
            f"Initialized a LLM app instance of LLMApp {model.model_dump_json(indent=2)}")
    else:
        raise RuntimeError("Not a LLM app instance were found.")

    user_config = model.model_dump()
    deployment_config = model.deployment_config.dict()
    deployment_config = deployment_config.copy()
    max_ongoing_requests = deployment_config.pop(
        "max_ongoing_requests", None
    ) or (user_config["model_conf"]["generation"].get("max_batch_size", 1) if user_config["model_conf"]["generation"] else 1)

    deployment = LLMDeployment.options(  # pylint:disable=no-member
        name=_reverse_prefix(model.model_conf.model_id),
        max_ongoing_requests=max_ongoing_requests,
        user_config=user_config,
        **deployment_config,
    ).bind()
    serve_conf = {
        "name": _reverse_prefix(model.model_conf.model_id)
    }

    return (ExperimentalDeployment.bind(deployment, model), serve_conf)  # pylint:disable=no-member


def run(models: Union[LLMApp, str], appname: str = None, port: int = DEFAULT_HTTP_PORT):
    """Run the LLM Server on the local Ray Cluster

    Args:
        *models: A list of LLMApp objects or paths to yaml files defining LLMApps

    Example:
       run("models/")           # run all models in the models directory
       run("models/model.yaml") # run one model in the model directory
       run({...LLMApp})         # run a single LLMApp
       run("models/model1.yaml", "models/model2.yaml", {...LLMApp}) # mix and match
    """
    app = llm_server(list(models))
    ray._private.usage.usage_lib.record_library_usage("llmserve")
    # serve_start_port = get_serve_start_port(port)
    # serve.start(http_options={"host": SERVE_RUN_HOST,
    #             "port": serve_start_port})
    if not appname:
        appname = "default"
    serve.run(app, name=appname, route_prefix="/api/v1/" + appname)


def run_experimental(models: Union[LLMApp, str], appname: str = None, port: int = DEFAULT_HTTP_PORT):
    """Run the LLM Server on the local Ray Cluster

    Args:
        model: A LLMApp objects or paths to yaml files defining LLMApps

    Example:
       run("models/")           # run all models in the models directory
       run("models/model.yaml") # run one model in the model directory
       run({...LLMApp})         # run a single LLMApp
       run("models/model1.yaml", "models/model2.yaml", {...LLMApp}) # mix and match
    """
    logger.info(f"begin time: {datetime.datetime.now()}")
    app = llm_experimental(list(models))
    serve_conf = app[1]
    ray._private.usage.usage_lib.record_library_usage("llmserve")
    serve_start_port = get_serve_start_port(port)
    logger.info(
        f"Serve run app at {SERVE_RUN_HOST}:{serve_start_port}/{serve_conf['name']}")
    serve.start(http_options={"host": SERVE_RUN_HOST,
                "port": serve_start_port})
    serve_name = appname + "-" + \
        serve_conf["name"] if appname else serve_conf["name"]
    serve.run(app[0], name=serve_name, route_prefix="/" + serve_name)
    logger.info(f"end time: {datetime.datetime.now()}")


def del_serve(app_name: str):
    serve.delete(app_name, _blocking=True)


def run_comparation():
    """Run the LLM Server on the local Ray Cluster

    Args:
        *models: A list of LLMApp objects or paths to yaml files defining LLMApps

    Example:
       run("models/")           # run all models in the models directory
       run("models/model.yaml") # run one model in the model directory
       run({...LLMApp})         # run a single LLMApp
       run("models/model1.yaml", "models/model2.yaml", {...LLMApp}) # mix and match
    """
    from llmserve.frontend.app import LLMServeFrontend
    serve_runtime_port = get_serve_port()
    cmp_serve_port = DEFAULT_HTTP_PORT
    if serve_runtime_port > -1:
        logger.info(
            f"Serve is already running at {SERVE_RUN_HOST}:{serve_runtime_port}")
        cmp_serve_port = serve_runtime_port
        serve.start(
            http_options={"host": SERVE_RUN_HOST, "port": serve_runtime_port})
    else:
        serve.start(
            http_options={"host": SERVE_RUN_HOST, "port": DEFAULT_HTTP_PORT})
    cmp_address = "/api/v1/default"
    logger.info(f"Bind LLMServeFrontend at {cmp_address}")
    app = LLMServeFrontend.options(  # pylint:disable=no-member
        ray_actor_options={"num_cpus": 1}, name="LLMServeFrontend").bind(cmp_address)
    ray._private.usage.usage_lib.record_library_usage("llmserve")
    logger.info(
        f"Serve run comparation running at {SERVE_RUN_HOST}/{cmp_serve_port}")
    serve.run(app, name="cmp_default", route_prefix="/cmp_default")


if __name__ == "__main__":
    run(*sys.argv[1:])
