import asyncio
import time
import traceback
from typing import Any, Dict, List, Optional, Union, Callable, Annotated, Tuple

import async_timeout
import json
import ray
import ray.util
from fastapi import FastAPI, Body, Header
from ray import serve
from ray.exceptions import RayActorError
from ray.serve.deployment import ClassNode

from llmserve.backend.llm.predictor import LLMPredictor
from llmserve.backend.logger import get_logger
# from llmserve.backend.server._batch import QueuePriority, _PriorityBatchQueue, batch
from llmserve.backend.server.exceptions import PromptTooLongError
from llmserve.backend.server.models import (
    Args,
    DeepSpeed,
    Prompt,
    InvokeParams,
    OpenParams,
)
from llmserve.backend.server.utils import parse_args, render_gradio_params
from llmserve.common.constants import GATEWAY_TIMEOUT_S
from ray.serve.gradio_integrations import GradioIngress
import gradio as gr
from fastapi.middleware.cors import CORSMiddleware

from llmserve.common.utils import _replace_prefix, _reverse_prefix

from starlette.responses import StreamingResponse
from typing import AsyncGenerator, Generator
from ray.serve.handle import DeploymentHandle, DeploymentResponseGenerator
from threading import Thread

logger = get_logger("ray.serve")
try:
    from vllm.engine.arg_utils import AsyncEngineArgs
    from vllm.engine.async_llm_engine import AsyncLLMEngine
    from vllm.entrypoints.openai.cli_args import make_arg_parser
    from vllm.entrypoints.openai.protocol import (
        ChatCompletionRequest,
        ChatCompletionResponse,
        ErrorResponse,
    )
    from vllm.entrypoints.openai.serving_chat import OpenAIServingChat
    from vllm.entrypoints.openai.serving_engine import LoRAModulePath
except ImportError:
    logger.info("Import vllm related stuff failed, please make sure 'vllm' is installed.")
   

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
        
@serve.deployment(
    autoscaling_config={
        "min_replicas": 1,
        "initial_replicas": 1,
        "max_replicas": 3,
        "target_ongoing_requests": 3, # the average number of ongoing requests per replica that the Serve autoscaler tries to ensure
    },
    max_ongoing_requests=5, # the maximum number of ongoing requests allowed for a replica
    health_check_period_s=10,
    health_check_timeout_s=30,
)
class LLMDeployment(LLMPredictor):
    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        logger.info('LLM Deployment initialize')
        self.args = None
        # Keep track of requests to cancel them gracefully
        self.requests_ids: Dict[int, bool] = {}
        self.curr_request_id: int = 0
        super().__init__()

    def _should_reinit_worker_group(self, new_args: Args) -> bool:
        logger.info('LLM Deployment _should_reinit_worker_group')
        old_args = self.args

        if not old_args:
            return True

        old_scaling_config = self.args.air_scaling_config
        new_scaling_config = new_args.scaling_config.as_air_scaling_config()

        if not self.base_worker_group:
            return True

        if old_scaling_config != new_scaling_config:
            return True

        if not old_args:
            return True

        if old_args.model_conf.initialization != new_args.model_conf.initialization:
            return True

        if (
            old_args.model_conf.generation.max_batch_size
            != new_args.model_conf.generation.max_batch_size
            and isinstance(new_args.model_conf.initialization.initializer, DeepSpeed)
        ):
            return True

        # TODO: Allow this
        if (
            old_args.model_conf.generation.prompt_format
            != new_args.model_conf.generation.prompt_format
        ):
            return True

        return False

    async def reconfigure(
        self,
        config: Union[Dict[str, Any], Args],
        force: bool = False,
    ) -> None:
        logger.info("LLM Deployment Reconfiguring...")
        if not isinstance(config, Args):
            new_args: Args = Args.model_validate(config)
        else:
            new_args: Args = config

        should_reinit_worker_group = force or self._should_reinit_worker_group(
            new_args)

        self.args = new_args
        if should_reinit_worker_group:
            await self.rollover(
                self.args.air_scaling_config,
                pg_timeout_s=self.args.scaling_config.pg_timeout_s,
            )
        self.update_batch_params(
            self.get_max_batch_size(), self.get_batch_wait_timeout_s())
        logger.info("LLM Deployment Reconfigured.")

    @property
    def max_batch_size(self):
        return (self.args.model_conf.generation.max_batch_size if self.args.model_conf.generation else 1)

    @property
    def batch_wait_timeout_s(self):
        return (self.args.model_conf.generation.batch_wait_timeout_s if self.args.model_conf.generation else 10)

    def get_max_batch_size(self):
        return self.max_batch_size

    def get_batch_wait_timeout_s(self):
        return self.batch_wait_timeout_s

    async def validate_prompt(self, prompt: Prompt) -> None:
        if len(prompt.prompt.split()) > self.args.model_conf.max_input_words:
            raise PromptTooLongError(
                f"Prompt exceeds max input words of "
                f"{self.args.model_conf.max_input_words}. "
                "Please make the prompt shorter."
            )

    @app.get("/metadata", include_in_schema=False)
    async def metadata(self) -> dict:
        return self.args.model_dump(
            exclude={
                "model_conf": {"initialization": {"s3_mirror_config", "runtime_env"}}
            }
        )

    @app.post("/", include_in_schema=False)
    async def generate_text(self, prompt: Prompt, generate: dict[str, str] = {}):
        if self.args.model_conf.model_task == "text-generation":
            await self.validate_prompt(prompt)
        
        with async_timeout.timeout(GATEWAY_TIMEOUT_S):
            text = await self.generate_text_batch(
                prompt,
                generate,
                # start_timestamp=start_timestamp,
            )
            logger.info(f"generated text: {text}")
            return text

    @app.post("/batch", include_in_schema=False)
    async def batch_generate_text(self, prompts: List[Prompt], generate: dict[str, str] = {}):
        logger.info(f"batch_generate_text prompts: {prompts} ")
        if self.args.model_conf.model_task == "text-generation":
            for prompt in prompts:
                await self.validate_prompt(prompt)
                
        time.time()
        with async_timeout.timeout(GATEWAY_TIMEOUT_S):
            texts = await asyncio.gather(
                *[
                    self.generate_text_batch(
                        prompt,
                        generate,
                        # start_timestamp=start_timestamp,
                    )
                    for prompt in prompts
                ]
            )
            return texts

    def update_batch_params(self, new_max_batch_size: int, new_batch_wait_timeout_s: float):
        self.generate_text_batch.set_max_batch_size(new_max_batch_size)
        self.generate_text_batch.set_batch_wait_timeout_s(
            new_batch_wait_timeout_s)
        self.stream_text_batch.set_max_batch_size(new_max_batch_size)
        self.stream_text_batch.set_batch_wait_timeout_s(
            new_batch_wait_timeout_s)
        logger.info(f"new_max_batch_size is {new_max_batch_size}")
        logger.info(f"new_batch_wait_timeout_s is {new_batch_wait_timeout_s}")

    @serve.batch(
        max_batch_size=2,
        batch_wait_timeout_s=0,
    )
    async def generate_text_batch(
        self,
        prompts: List[Prompt],
        generate: dict[str, str] = {},
        *,
        start_timestamp: Optional[Union[float, List[float]]] = None,
        timeout_s: Union[float, List[float]] = GATEWAY_TIMEOUT_S - 10,
    ):
        """Generate text from the given prompts in batch.

        Args:
            prompts (List[Prompt]): Batch of prompts to generate text from.
            start_timestamp (Optional[float], optional): Timestamp of when the
                batch was created. Defaults to None. If set, will early stop
                the generation.
            timeout_s (float, optional): Timeout for the generation. Defaults
                to GATEWAY_TIMEOUT_S-10. Ignored if start_timestamp is None.
        """
        if not prompts or prompts[0] is None:
            return prompts

        if isinstance(start_timestamp, list) and start_timestamp[0]:
            start_timestamp = min(start_timestamp)
        elif isinstance(start_timestamp, list):
            start_timestamp = start_timestamp[0]
        if isinstance(timeout_s, list) and timeout_s[0]:
            timeout_s = min(timeout_s)
        elif isinstance(timeout_s, list):
            timeout_s = timeout_s[0]

        logger.info(
            f"Received {len(prompts)} prompts {prompts}. start_timestamp {start_timestamp} timeout_s {timeout_s}"
        )

        data_ref = ray.put(prompts)
        generate_ref = ray.put(generate)

        while not self.base_worker_group:
            logger.info("Waiting for worker group to be initialized...")
            await asyncio.sleep(1)

        try:
            prediction = await self._predict_async(
                data_ref, generate_ref, timeout_s=timeout_s, start_timestamp=start_timestamp
            )
        except RayActorError as e:
            raise RuntimeError(
                f"Prediction failed due to RayActorError. "
                "This usually means that one or all prediction workers are dead. "
                "Try again in a few minutes. "
                f"Traceback:\n{traceback.print_exc()}"
            ) from e

        logger.info(f"Predictions {prediction}")
        if not isinstance(prediction, list):
            return [prediction]
        return prediction[: len(prompts)]


    # async def pre_stream_text_batch(
    #     self,
    #     prompt: Prompt,
    #     request: Request,
    #     *,
    #     start_timestamp: Optional[Union[float, List[float]]] = None,
    #     timeout_s: Union[float, List[float]] = GATEWAY_TIMEOUT_S - 10,
    #     **kwargs,
    # ) -> AsyncGenerator:
    #     """Generate text from the given prompts in batch.

    #     Args:
    #         prompts (List[Prompt]): Batch of prompts to generate text from.
    #         start_timestamp (Optional[float], optional): Timestamp of when the
    #             batch was created. Defaults to None. If set, will early stop
    #             the generation.
    #         timeout_s (float, optional): Timeout for the generation. Defaults
    #             to GATEWAY_TIMEOUT_S-10. Ignored if start_timestamp is None.
    #         **kwargs: Additional arguments to pass to the batch function.
    #     """
    #     curr_request_id = self.curr_request_id
    #     self.requests_ids[curr_request_id] = False
    #     self.curr_request_id += 1
    #     async_generator = self._generate_text_batch(
    #         (prompt, curr_request_id),
    #         start_timestamp=start_timestamp,
    #         timeout_s=timeout_s,
    #         **kwargs,
    #     )
    #     # The purpose of this loop is to ensure that the underlying
    #     # generator is fully consumed even if the client disconnects.
    #     # If the loop is not consumed, then the PredictionWorker will
    #     # be stuck.
    #     # TODO: Revisit this - consider catching asyncio.CancelledError
    #     # and/or setting a Ray Event to cancel the PredictionWorker generator.
    #     while True:
    #         try:
    #             future = async_generator.__anext__()

    #             if not self.requests_ids[curr_request_id]:
    #                 future = asyncio.ensure_future(future)
    #                 done, pending = await asyncio.wait(
    #                     (future, _until_disconnected(request)),
    #                     return_when=asyncio.FIRST_COMPLETED,
    #                 )
    #                 if future in done:
    #                     yield await future
    #                 else:
    #                     # We caught the disconnect
    #                     logger.info(f"Request {curr_request_id} disconnected.")
    #                     self.requests_ids[curr_request_id] = True
    #             else:
    #                 await future
    #         except StopAsyncIteration:
    #             break
    #     del self.requests_ids[curr_request_id]

    @serve.batch(
        max_batch_size=2,
        batch_wait_timeout_s=0,
    )
    async def stream_text_batch(
        self,
        prompts_and_request_ids: List[Tuple[Prompt, int, dict]],
        *,
        start_timestamp: Optional[Union[float, List[float]]] = None,
        timeout_s: Union[float, List[float]] = GATEWAY_TIMEOUT_S - 10,
    ):
        """Generate text from the given prompts in batch.

        Args:
            prompts (List[Prompt]): Batch of prompts to generate text from.
            start_timestamp (Optional[float], optional): Timestamp of when the
                batch was created. Defaults to None. If set, will early stop
                the generation.
            timeout_s (float, optional): Timeout for the generation. Defaults
                to GATEWAY_TIMEOUT_S-10. Ignored if start_timestamp is None.
        """
        prompts, request_ids, generate = zip(*prompts_and_request_ids)
        # get tuple, need extract it
        prompts_plain = [p for prompt in prompts for p in prompt]
        request_ids_plain = list(request_ids)
        generate_plain = list(generate)

        if isinstance(start_timestamp, list) and start_timestamp[0]:
            start_timestamp = min(start_timestamp)
        elif isinstance(start_timestamp, list):
            start_timestamp = start_timestamp[0]
        if isinstance(timeout_s, list) and timeout_s[0]:
            timeout_s = min(timeout_s)
        elif isinstance(timeout_s, list):
            timeout_s = timeout_s[0]

        logger.info(
            f"Received {len(prompts_plain)} prompts {prompts_plain}. start_timestamp {start_timestamp} timeout_s {timeout_s}"
        )

        data_ref = ray.put(prompts_plain)
        generate_ref = ray.put(generate_plain)

        while not self.base_worker_group:
            logger.info("Waiting for worker group to be initialized...")
            await asyncio.sleep(1)

        try:
            async for result in self._stream_async(
                data_ref,
                generate_ref,
                timeout_s=timeout_s,
                start_timestamp=start_timestamp,
            ):
                yield [
                    v if v is not None or self.requests_ids[id] else StopIteration
                    for v, id in zip(result, request_ids)
                ]
        except RayActorError as e:
            raise RuntimeError(
                f"Prediction failed due to RayActorError. "
                "This usually means that one or all prediction workers are dead. "
                "Try again in a few minutes. "
                f"Traceback:\n{traceback.print_exc()}"
            ) from e
        finally:
            logger.info(f"Batch for {request_ids_plain} finished")
     
    async def stream_generate_text(self, prompt: Union[Prompt, List[Prompt]], generate: dict[str, str] = {}) -> Generator[str, None, None]:
        logger.info(f"call LLMPredictor.stream_generate_texts")
        curr_request_id = self.curr_request_id
        self.requests_ids[curr_request_id] = True
        self.curr_request_id += 1
        if not isinstance(prompt, list):
            prompt = [prompt]

        async for s in self.stream_text_batch((prompt, curr_request_id, generate)):
            yield s

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}:{self.args.model_conf.model_id}"

@serve.deployment(
    # TODO: make this configurable in llmserve run
    autoscaling_config={
        "min_replicas": 1,
        "initial_replicas": 1,
        "max_replicas": 3,
        "target_ongoing_requests": 10, # the average number of ongoing requests per replica that the Serve autoscaler tries to ensure
    },
    max_ongoing_requests=30,  # the maximum number of ongoing requests allowed for a replica
)
@serve.ingress(app)
class RouterDeployment:
    def __init__(self, models: Dict[str, DeploymentHandle], model_configurations: Dict[str, Args]) -> None:
        self._models = models
        # TODO: Remove this once it is possible to reconfigure models on the fly
        self._model_configurations = model_configurations
        logger.info(f"init: _models.keys: {self._models.keys()}")

    @app.post("/{model}/run/predict")
    async def predict(
            self, 
            model: str, 
            params: InvokeParams
            ) -> Union[Dict[str, Any], List[Dict[str, Any]], List[Any]]:
        prompt = params.prompt
        if not isinstance(prompt, list):
            prompt = [prompt]
        prompt = [p if isinstance(p, Prompt) else Prompt(prompt=p, use_prompt_format=False) for p in prompt]

        generate_kwargs = params.dict(exclude={"prompt"}, exclude_none=True)
        logger.info(f"get generation params: {generate_kwargs}")
        logger.info(f"url: {model}, keys: {self._models.keys()}")
        modelKeys = list(self._models.keys())

        modelID = model
        for item in modelKeys:
            logger.info(f"_reverse_prefix(item): {_reverse_prefix(item)}")
            if _reverse_prefix(item) == model:
                modelID = item
                logger.info(f"set modelID: {item}")
        logger.info(f"search model key {modelID}")

        if isinstance(prompt, Prompt):
            results = await asyncio.gather(*[self._models[modelID].options(stream=False).generate_text.remote(prompt, generate_kwargs)])
        elif isinstance(prompt, list):
            results = await asyncio.gather(*[self._models[modelID].options(stream=False).batch_generate_text.remote(prompt, generate_kwargs)])
        else:
            raise Exception("Invaid prompt format.")
        
        logger.info(f"{results}")
        return results[0]

    @app.get("/{model}/metadata")
    async def metadata(self, model: str) -> Dict[str, Dict[str, Any]]:
        model = _replace_prefix(model)
        # This is what we want to do eventually, but it looks like reconfigure is blocking
        # when called on replica init
        # metadata = await asyncio.gather(
        #     *(await asyncio.gather(*[self._models[model].metadata.remote()]))
        # )
        # metadata = metadata[0]
        metadata = self._model_configurations[model].model_dump(
            exclude={
                "model_conf": {"initialization": {"s3_mirror_config", "runtime_env"}}
            }
        )
        logger.info(metadata)
        return {"metadata": metadata}

    @app.get("/models")
    async def models(self) -> List[str]:
        return list(self._models.keys())

    @app.post("/{model}/run/stream") 
    def stream(self, model: str, params: InvokeParams) -> StreamingResponse:
        prompt = params.prompt
        if not isinstance(prompt, list):
            prompt = [prompt]
        prompt = [p if isinstance(p, Prompt) else Prompt(prompt=p, use_prompt_format=False) for p in prompt]

        generate_kwargs = params.dict(exclude={"prompt"}, exclude_none=True)
        logger.info(f"get generation params: {generate_kwargs}")
        logger.info(f"url: {model}, keys: {self._models.keys()}")
            
        modelKeys = list(self._models.keys())
        modelID = model
        for item in modelKeys:
            logger.info(f"_reverse_prefix(item): {_reverse_prefix(item)}")
            if _reverse_prefix(item) == model:
                modelID = item
                logger.info(f"set stream model id: {item}")

        logger.info(f"search stream model key: {modelID}")
        return StreamingResponse(self.stream_generate_text(modelID, prompt, generate_kwargs), media_type="text/plain")

    async def stream_generate_text(self, modelID: str, prompt: Union[Prompt, List[Prompt]], generate: dict[str, str] = {}) -> AsyncGenerator[str, None]:
        logger.info(f'streamer_generate_text: {modelID}, prompt: {prompt}')
        r: DeploymentResponseGenerator = self._models[modelID].options(stream=True).stream_generate_text.remote(prompt, generate)
        async for i in r:
            yield i.generated_text

    @app.post("/{model}/chat/completions")
    async def chat_completions(self, model: str, params: OpenParams) -> Any:
        logger.info(f"stream: {params.stream}")
        count = len(params.messages)
        if count < 1:
            raise Exception("Invaid prompt format.")
        
        prompt = [params.messages[-1].content]
        prompt = [p if isinstance(p, Prompt) else Prompt(prompt=p, use_prompt_format=False) for p in prompt]

        generate_kwargs = params.dict(exclude={"messages", "model", "stream"}, exclude_none=True)
        logger.info(f"get generation params: {generate_kwargs}")
        logger.info(f"url: {model}, keys: {self._models.keys()}")
        
        modelKeys = list(self._models.keys())
        modelID = model
        for item in modelKeys:
            logger.info(f"_reverse_prefix(item): {_reverse_prefix(item)}")
            if _reverse_prefix(item) == model:
                modelID = item
                logger.info(f"set modelID: {item}")
        logger.info(f"search model key {modelID}")

        if params.stream:
            return StreamingResponse(self.do_stream_generate_text(modelID, prompt, generate_kwargs), media_type="text/plain")
        else:
            return await self.do_chat_completions(modelID, model, prompt, generate_kwargs)

    async def do_stream_generate_text(self, modelID: str, prompt: Union[Prompt, List[Prompt]], generate: dict[str, str] = {}) -> AsyncGenerator[str, None]:
        logger.info(f'streamer_generate_text: {modelID}, prompt: {prompt}')
        r: DeploymentResponseGenerator = self._models[modelID].options(stream=True).stream_generate_text.remote(prompt, generate)
        index = 0
        async for i in r:
            yield self.convertToStreamResult(modelID, index, i.generated_text, None)
            index += 1
        yield self.convertToStreamResult(modelID, index, "", "stop")
    
    async def do_chat_completions(self, modelID: str, model: str, prompt: Union[Prompt, List[Prompt]], generate_kwargs: dict[str, str] = {})-> Union[Dict[str, Any], List[Dict[str, Any]], List[Any]]:
        if isinstance(prompt, list):
            results = await asyncio.gather(*[self._models[modelID].options(stream=False).batch_generate_text.remote(prompt, generate_kwargs)])
        else:
            raise Exception("Invaid prompt format.")
        logger.info(f"{results}")
        result = self.convertToOpenResult(model, results[0])
        return result        
    
    def convertToOpenResult(self, model: str, result) -> Any:
        result = result[0]
        usage = result.dict(exclude={"generated_text"}, exclude_none=True)
        returnJson = self.convertToJson("chat.completion", "message", model, usage, 0, result.generated_text, "stop")
        return returnJson
    
    def convertToStreamResult(self, model: str, index: int, result: str, finishReason: Union[str, None]) -> Any:
        returnJson = self.convertToJson("chat.completion.chunk", "delta", model, None, index, result, finishReason)
        return json.dumps(returnJson)
    
    def convertToJson(self, object: str, keyName: str, model: str, usage: Union[dict, None], index: int, result: str, finishReason: Union[str, None]) -> Any:
        returnVal = {
            "object": object,
            "created": time.time(),
            "model": model,
            "usage": usage,
            "choices":[
                {
                    "index": index,
                    keyName: {"role":"assistant", "content": result},
                    "finish_reason": finishReason
                 }
            ]
        }
        return returnVal

@serve.deployment(
    # TODO: make this configurable in llmserve run
    autoscaling_config={
        "min_replicas": 1,
        "initial_replicas": 1,
        "max_replicas": 2,
        "target_ongoing_requests": 3,
    },
    max_ongoing_requests=5,  # the maximum number of ongoing requests allowed for a replica
)
class ExperimentalDeployment(GradioIngress):
    def __init__(
        self, model: ClassNode, model_configuration: Args
    ) -> None:
        logger.info('Experiment Deployment Initialize')
        self._model = model
        # TODO: Remove this once it is possible to reconfigure models on the fly
        self._model_configuration = model_configuration
        self.batch_size = self._model_configuration.model_conf.generation.max_batch_size if self._model_configuration.model_conf.generation else 1
        self.hg_task = self._model_configuration.model_conf.model_task
        pipeline_info = render_gradio_params(self.hg_task)

        self.pipeline_info = pipeline_info
        super().__init__(self._chose_ui())

    async def query(self, *args) -> Dict[str, Dict[str, Any]]:
        if args[0] is None:
            return None
        
        logger.info(f"ExperimentalDeployment query.args {args}")
        if len(args) > 1:
            prompts = args
        else:
            prompts = args[0]
        logger.info(f"ExperimentalDeployment query.prompts {prompts}")
        use_prompt_format = False
        if self._model_configuration.model_conf.generation and self._model_configuration.model_conf.generation.prompt_format:
            use_prompt_format = True
        results = await asyncio.gather(*[(self._model.generate_text.remote(Prompt(prompt=prompts, use_prompt_format=use_prompt_format)))])
        logger.info(f"ExperimentalDeployment query.results {results}")
        results = results[0]
        return results
    

    async def stream(self, *args) -> AsyncGenerator[str, None]:
        if args[0] is None:
            yield StopIteration
        
        logger.info(f"ExperimentalDeployment query.args {args}")
        prompts = args[0]

        logger.info(f"prompt is {prompts}")
        content = ""
        r: DeploymentResponseGenerator = self._model.options(stream=True).stream_generate_text.remote(Prompt(prompt=prompts, use_prompt_format=True))
        async for i in r:
            content += i.generated_text
            yield content

    def _chose_ui(self) -> Callable:
        logger.info(
            f'Experiment Deployment chose ui for {self._model_configuration.model_conf.model_id}')

        gr_params = self.pipeline_info
        del gr_params["preprocess"]
        del gr_params["postprocess"]
        if self.hg_task == "text-generation":
            return lambda: gr.ChatInterface(self.stream, concurrency_limit=self.batch_size).queue()
        else:
            return lambda: gr.Interface(self.query, **gr_params, title=self._model_configuration.model_conf.model_id)

@serve.deployment(
    autoscaling_config={
        "min_replicas": 1,
        "max_replicas": 10,
        "target_ongoing_requests": 5,
    },
    max_ongoing_requests=10,
)
class VLLMDeployment:
    def __init__(
        self,
        # test: str,
        # model_configuration: dict,
        # config: Union[Dict[str, Any], Args]
        # engine_args: AsyncEngineArgs,
        # response_role: str,
        # lora_modules: Optional[List[LoRAModulePath]] = None,
        # chat_template: Optional[str] = None,
    ):
        pass
        # logger.info(f"Starting with engine args: {engine_args}")
        # self.openai_serving_chat = None
        # self.engine_args = engine_args
        # self.response_role = response_role
        # self.lora_modules = lora_modules
        # self.chat_template = chat_template
        # logger.info("^^^^^^^^^^^^^^^^^^^^^^^^^")
        # logger.info(model_configuration)
        # logger.info("LLM Deployment Reconfiguring...")
        # if not isinstance(config, Args):
        #     new_args: Args = Args.model_validate(config)
        # else:
        #     new_args: Args = config

        # self.args = new_args
        # self.update_batch_params(
        #     self.get_max_batch_size(), self.get_batch_wait_timeout_s())
        # logger.info("******************")
        # logger.info(engine_args)
        # print("******************")
        # print(engine_args)
        
        # self.engine = AsyncLLMEngine.from_engine_args(engine_args)


        # model_config = await self.engine.get_model_config()
        # # Determine the name of the served model for the OpenAI client.
        # if self.engine_args.served_model_name is not None:
        #     served_model_names = self.engine_args.served_model_name
        # else:
        #     served_model_names = [self.engine_args.model]
        # self.openai_serving_chat = OpenAIServingChat(
        #     self.engine, model_config, served_model_names, self.response_role, self.lora_modules, self.chat_template
        # )
        # logger.info("LLM Deployment Reconfigured.")
    
    def _get_vllm_engine_config(self, args: dict) -> AsyncEngineArgs:
        # Generate engine arguements and engine configs
        # model_id_or_path = get_model_location_on_disk(args.model_conf.actual_hf_model_id)
        # new_args = Args.model_validate(args)
        # new_args = Args(args)
        logger.info("666")
        logger.info(args)
        async_engine_args = AsyncEngineArgs(
            # This is the local path on disk, or the hf model id
            # If it is the hf_model_id, vllm automatically downloads the correct model.
            **dict(
                model=args.get("model_conf").get("model_id"),
                # model="THUDM/chatglm3-6b",
                worker_use_ray=True,
                engine_use_ray=False,
                tensor_parallel_size=args.get("scaling_config").get("num_workers"),
                # tensor_parallel_size=2,
                max_model_len=args.get("model_conf").get("max_input_words", 128),
                # max_model_len=128,
                disable_log_stats=False,
                max_log_len=64,
                # trust_remote_code=True,
                # **args.model_conf.initialization.initializer.get_initializer_kwargs(),
                **args.get("model_conf").get("initialization").get("initializer").get("from_pretrained_kwargs"),
            )
        )

        return async_engine_args
    
    async def reconfigure(
        self,
        config: dict,
        # config: Union[dict, Args],
    ) -> None:
        logger.info("^^^^^^^^^^^^^^^^^^^^^^^^^")
        logger.info("LLM Deployment Reconfiguring...")
        # if not isinstance(config, Args):
        #     new_args: Args = Args.model_validate(config)
        #     logger.info("1111111222222222")
        # else:
        #     new_args: Args = config

        # self.args = new_args
        # self.update_batch_params(
        #     self.get_max_batch_size(), self.get_batch_wait_timeout_s())

        # logger.info(type(new_args))
        
        engine_args = self._get_vllm_engine_config(config)
        logger.info("******************")
        logger.info(engine_args)
        print("******************")
        print(engine_args)
        
        self.engine = AsyncLLMEngine.from_engine_args(engine_args)
        # model_config = await self.engine.get_model_config()
        # # Determine the name of the served model for the OpenAI client.
        # if self.engine_args.served_model_name is not None:
        #     served_model_names = self.engine_args.served_model_name
        # else:
        #     served_model_names = [self.engine_args.model]
        # self.openai_serving_chat = OpenAIServingChat(
        #     self.engine, model_config, served_model_names, self.response_role, self.lora_modules, self.chat_template
        # )
        logger.info("LLM Deployment Reconfigured.")
        model_config = await self.engine.get_model_config()
        # Determine the name of the served model for the OpenAI client.
        # if self.engine_args.served_model_name is not None:
        #     served_model_names = self.engine_args.served_model_name
        # else:
        #     served_model_names = [self.engine_args.model]

        chat_template = config.get("model_conf").get("generation").get("prompt_format")
        served_model_names = [engine_args.model]
        logger.info(engine_args.model)

        logger.info(type(self.engine))
        logger.info(type(model_config))
        logger.info(type(served_model_names))
        logger.info(type(chat_template))

        self.openai_serving_chat = OpenAIServingChat(
            self.engine,
            model_config,
            served_model_names, 
            "assistant",
            None,
            chat_template
        )
        # self.openai_serving_completion = OpenAIServingCompletion(
        # engine, model_config, served_model_names, args.lora_modules)
    
    # async def create_chat_completion(
    #     self, request: ChatCompletionRequest, raw_request: Request
    # ):
    #     """OpenAI-compatible HTTP endpoint.

    #     API reference:
    #         - https://docs.vllm.ai/en/latest/serving/openai_compatible_server.html
    #     """
    #     if not self.openai_serving_chat:
    #         model_config = await self.engine.get_model_config()
    #         # Determine the name of the served model for the OpenAI client.
    #         if self.engine_args.served_model_name is not None:
    #             served_model_names = self.engine_args.served_model_name
    #         else:
    #             served_model_names = [self.engine_args.model]
    #         self.openai_serving_chat = OpenAIServingChat(
    #             self.engine, model_config, served_model_names, self.response_role, self.lora_modules, self.chat_template
    #         )
    #     logger.info(f"Request: {request}")
    #     generator = await self.openai_serving_chat.create_chat_completion(
    #         request, raw_request
    #     )
    #     if isinstance(generator, ErrorResponse):
    #         return JSONResponse(
    #             content=generator.model_dump(), status_code=generator.code
    #         )
    #     if request.stream:
    #         return StreamingResponse(content=generator, media_type="text/event-stream")
    #     else:
    #         assert isinstance(generator, ChatCompletionResponse)
    #         return JSONResponse(content=generator.model_dump())