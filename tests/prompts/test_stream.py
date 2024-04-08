import json
import logging
import os
from time import sleep
import time

from pytest import fixture
from flow_prompt import FlowPrompt, behaviour, PipePrompt, AttemptToCall, AzureAIModel, C_128K
logger = logging.getLogger(__name__)


@fixture
def fp():
    azure_keys = json.loads(os.getenv("AZURE_KEYS", "{}"))
    flow_prompt = FlowPrompt(azure_keys=azure_keys)
    return flow_prompt

@fixture
def gpt4_behaviour():
    return behaviour.AIModelsBehaviour(
        attempts=[
            AttemptToCall(
                ai_model=AzureAIModel(
                    realm='westus',
                    deployment_id="gpt-4-turbo",
                    max_tokens=C_128K,
                    support_functions=True,
                    should_verify_client_has_creds=False,
                ),
                weight=100,
            ),
        ]
    )

def stream_function(text, **kwargs):
    print(text)

def stream_check_connection(validate, **kwargs):
    return validate

def _test_loading_prompt_from_service(fp, gpt4_behaviour):

    context = {
        'messages': ['test1', 'test2'],
        'assistant_response_in_progress': None,
        'files': ['file1', 'file2'],
        'music': ['music1', 'music2'],
        'videos': ['video1', 'video2'],
    }

    # initial version of the prompt
    prompt_id = f'test-{time.time()}'
    fp.service.clear_cache()
    prompt = PipePrompt(id=prompt_id) 
    prompt.add("It's a system message, Hello {name}", role="system")
    prompt.add('{messages}', is_multiple=True, in_one_message=True, label='messages')
    fp.call(prompt.id, context, gpt4_behaviour, stream_function=stream_function, check_connection=stream_check_connection, params={"stream": True}, stream_params={"validate": False})