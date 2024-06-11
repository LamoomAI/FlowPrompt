import logging
import typing as t
from dataclasses import dataclass
from decimal import Decimal

from openai import AzureOpenAI, OpenAI

from flow_prompt import Secrets, settings
from flow_prompt.ai_models.ai_model import AI_MODELS_PROVIDER
from flow_prompt.ai_models.attempt_to_call import AttemptToCall
from flow_prompt.ai_models.behaviour import AIModelsBehaviour, PromptAttempts
from flow_prompt.exceptions import (
    FlowPromptIsnotFoundException,
    RetryableCustomException,
)
from flow_prompt.services.SaveWorker import SaveWorker
from flow_prompt.prompt.pipe_prompt import PipePrompt
from flow_prompt.prompt.user_prompt import UserPrompt
from flow_prompt.responses import AIResponse
from flow_prompt.services.flow_prompt import FlowPromptService
from flow_prompt.utils import current_timestamp_ms

logger = logging.getLogger(__name__)


@dataclass
class FlowPrompt:
    api_token: str = None
    openai_key: str = None
    openai_org: str = None
    claude_key: str = None
    gemini_key: str = None
    azure_keys: t.Dict[str, str] = None
    secrets: Secrets = None

    def __post_init__(self):
        self.secrets = Secrets()
        if not self.azure_keys:
            if self.secrets.azure_keys:
                logger.debug(f"Using Azure keys from secrets")
                self.azure_keys = self.secrets.azure_keys
            else:
                logger.debug(f"Azure keys not found in secrets")
        if not self.api_token and self.secrets.API_TOKEN:
            logger.debug(f"Using API token from secrets")
            self.api_token = self.secrets.API_TOKEN
        if not self.openai_key and self.secrets.OPENAI_API_KEY:
            logger.debug(f"Using OpenAI API key from secrets")
            self.openai_key = self.secrets.OPENAI_API_KEY
        if not self.openai_org and self.secrets.OPENAI_ORG:
            logger.debug(f"Using OpenAI organization from secrets")
            self.openai_org = self.secrets.OPENAI_ORG
        self.service = FlowPromptService()
        if self.openai_key:
            openai_client = OpenAI(
                organization=self.openai_org,
                api_key=self.openai_key,
            )
            logger.debug(f"Initialized OpenAI client: {openai_client}")
            settings.AI_CLIENTS[AI_MODELS_PROVIDER.OPENAI] = openai_client
        if self.azure_keys:
            settings.AI_CLIENTS[AI_MODELS_PROVIDER.AZURE] = {}
            for realm, key_data in self.azure_keys.items():
                if realm in settings.AI_CLIENTS[AI_MODELS_PROVIDER.AZURE]:
                    logger.warning(
                        f"Realm {realm} already initialized. Rewriting it with new data"
                    )
                settings.AI_CLIENTS[AI_MODELS_PROVIDER.AZURE][realm] = AzureOpenAI(
                    api_version=key_data.get("api_version", "2023-07-01-preview"),
                    azure_endpoint=key_data["url"],
                    api_key=key_data["key"],
                )
                logger.debug(f"Initialized Azure client for {realm} {key_data['url']}")
        
        settings.AI_KEYS[AI_MODELS_PROVIDER.CLAUDE] = self.claude_key
        settings.AI_KEYS[AI_MODELS_PROVIDER.GEMINI] = self.gemini_key
        self.worker = SaveWorker()

    def call(
        self,
        prompt_id: str,
        context: t.Dict[str, str],
        behaviour: AIModelsBehaviour,
        params: t.Dict[str, t.Any] = {},
        version: str = None,
        count_of_retries: int = None,
        stream_function: t.Callable = None,
        check_connection: t.Callable = None,
        stream_params: dict = {},
    ) -> AIResponse:
        """
        Call flow prompt with context and behaviour
        """

        logger.debug(f"Calling {prompt_id}")
        start_time = current_timestamp_ms()
        pipe_prompt = self.get_pipe_prompt(prompt_id, version)
        prompt_attempts = PromptAttempts(behaviour, count_of_retries=count_of_retries)

        while prompt_attempts.initialize_attempt():
            current_attempt = prompt_attempts.current_attempt
            user_prompt = pipe_prompt.create_prompt(current_attempt)
            calling_messages = user_prompt.resolve(context)
            try:
                result = current_attempt.ai_model.call(
                    calling_messages.get_messages(),
                    calling_messages.max_sample_budget,
                    stream_function = stream_function,
                    check_connection = check_connection,
                    stream_params = stream_params,
                    **params,
                )
                
                sample_budget = self.calculate_budget_for_text(
                        user_prompt, result.get_message_str()
                    )
                result.metrics.price_of_call = self.get_price(
                    current_attempt,
                    sample_budget,
                    calling_messages.prompt_budget,
                )
                result.metrics.sample_tokens_used = sample_budget
                result.metrics.prompt_tokens_used = calling_messages.prompt_budget
                result.metrics.ai_model_details = current_attempt.ai_model.get_metrics_data()
                result.metrics.latency = current_timestamp_ms() - start_time

                if settings.USE_API_SERVICE and self.api_token:
                    self.worker.add_task(
                        self.api_token,
                        pipe_prompt.service_dump(),
                        context,
                        result,
                    )
                
                return result
            except RetryableCustomException as e:
                logger.error(f"Attempt failed: {prompt_attempts.current_attempt} with retryable error: {e}")
            except Exception as e:
                logger.exception(f"Attempt failed: {prompt_attempts.current_attempt} with non-retryable error: {e}")
                raise e

    def get_pipe_prompt(self, prompt_id: str, version: str = None) -> PipePrompt:
        """
        if the user has keys:  lib -> service: get_actual_prompt(local_prompt) -> Service:
        generates hash of the prompt;
        check in Redis if that record is the latest; if yes -> return 200, else
        checks if that record exists with that hash;
        if record exists and it's not the last - then we load the latest published prompt; - > return  200 + the last record
        add a new record in storage, and adding that it's the latest published prompt; -> return 200
        update redis with latest record;
        """
        logger.debug(f"Getting pipe prompt {prompt_id}")
        if settings.USE_API_SERVICE and self.api_token:
            prompt_data = None
            prompt = settings.PIPE_PROMPTS.get(prompt_id)
            if prompt:
                prompt_data = prompt.service_dump()
            try:
                response = self.service.get_actual_prompt(
                    self.api_token, prompt_id, prompt_data, version
                )
                if not response.is_taken_globally:
                    prompt.version = response.version
                    return prompt
                response.prompt['version'] = response.version
                return PipePrompt.service_load(response.prompt)
            except Exception as e:
                logger.exception(f"Error while getting prompt {prompt_id}: {e}")
                if prompt:
                    return prompt
                else:
                    logger.exception(f"Prompt {prompt_id} not found")
                    raise FlowPromptIsnotFoundException()
                
        else:
            return settings.PIPE_PROMPTS[prompt_id]

    def calculate_budget_for_text(self, user_prompt: UserPrompt, text: str) -> int:
        if not text:
            return 0
        return len(user_prompt.encoding.encode(text))

    def _decimal(self, value) -> Decimal:
        return Decimal(value).quantize(Decimal(".00001"))

    def get_price(
        self, attempt: AttemptToCall, sample_budget: int, prompt_budget: int
    ) -> Decimal:
        return self._decimal(
            prompt_budget * attempt.ai_model.price_per_prompt_1k_tokens / 1000
        ) + self._decimal(
            sample_budget * attempt.ai_model.price_per_sample_1k_tokens / 1000
        )
