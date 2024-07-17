"""
Test the OpenAI generator endpoints
"""

import shutil
import tempfile

from openai import AzureOpenAI, InternalServerError, RateLimitError
import pytest
from pytest_httpserver import HTTPServer

from .test_uvicorn_server import UvicornTestServer

from aoai_simulated_api.record_replay.handler import get_default_forwarders
from aoai_simulated_api.models import (
    Config,
    LatencyConfig,
    ChatCompletionLatency,
    CompletionLatency,
    EmbeddingLatency,
    OpenAIDeployment,
)


class TempDirectory:
    _temp_dir: str | None = None
    _prefix: str

    def __init__(self, prefix: str = "aoai-simulated-api-test-"):
        self._prefix = prefix

    def __enter__(self):
        self._temp_dir = tempfile.mkdtemp(prefix=self._prefix)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):

        shutil.rmtree(self._temp_dir)

    @property
    def path(self):
        return self._temp_dir


API_KEY = "123456879"


def _get_record_config(httpserver: HTTPServer, recording_path: str) -> Config:
    forwarding_server_url = httpserver.url_for("/").removesuffix("/")
    config = Config(generators=[])
    config.simulator_api_key = API_KEY
    config.simulator_mode = "record"
    config.recording.aoai_api_endpoint = forwarding_server_url
    config.recording.aoai_api_key = "123456789"
    config.recording.dir = recording_path
    config.recording.forwarders = get_default_forwarders()
    config.latency = LatencyConfig(
        open_ai_completions=CompletionLatency(
            LATENCY_OPENAI_COMPLETIONS_MEAN=0,
            LATENCY_OPENAI_COMPLETIONS_STD_DEV=0.1,
        ),
        open_ai_chat_completions=ChatCompletionLatency(
            LATENCY_OPENAI_CHAT_COMPLETIONS_MEAN=0,
            LATENCY_OPENAI_CHAT_COMPLETIONS_STD_DEV=0.1,
        ),
        open_ai_embeddings=EmbeddingLatency(
            LATENCY_OPENAI_EMBEDDINGS_MEAN=0,
            LATENCY_OPENAI_EMBEDDINGS_STD_DEV=0.1,
        ),
    )
    return config


def _get_replay_config(recording_path: str) -> Config:
    config = Config(generators=[])
    config.simulator_api_key = API_KEY
    config.simulator_mode = "replay"
    config.recording.dir = recording_path
    config.recording.forwarders = get_default_forwarders()
    config.latency = LatencyConfig(
        open_ai_completions=CompletionLatency(
            LATENCY_OPENAI_COMPLETIONS_MEAN=0,
            LATENCY_OPENAI_COMPLETIONS_STD_DEV=0.1,
        ),
        open_ai_chat_completions=ChatCompletionLatency(
            LATENCY_OPENAI_CHAT_COMPLETIONS_MEAN=0,
            LATENCY_OPENAI_CHAT_COMPLETIONS_STD_DEV=0.1,
        ),
        open_ai_embeddings=EmbeddingLatency(
            LATENCY_OPENAI_EMBEDDINGS_MEAN=0,
            LATENCY_OPENAI_EMBEDDINGS_STD_DEV=0.1,
        ),
    )
    config.openai_deployments = {
        "low_limit": OpenAIDeployment(name="low_limit", model="gpt-3.5-turbo", tokens_per_minute=64 * 6)
    }
    return config


@pytest.mark.asyncio
async def test_openai_record_replay_completion(httpserver: HTTPServer):
    """
    Ensure we can call the completion endpoint using the record mode
    and then replay the same recording in replay mode
    """

    # Set up pytest-httpserver to provide an endpoint for the simulator
    # to call in record mode
    httpserver.expect_request(
        uri="/openai/deployments/deployment1/completions",
        query_string="api-version=2023-12-01-preview",
        method="POST",
    ).respond_with_data(
        '{"id":"cmpl-95FbXadIqJEMZZ1Rl0chTcKRxk2ez","object":"text_completion","created":1711038651,"model":"gpt-35-turbo","prompt_filter_results":[{"prompt_index":0,"content_filter_results":{"hate":{"filtered":false,"severity":"safe"},"self_harm":{"filtered":false,"severity":"safe"},"sexual":{"filtered":false,"severity":"safe"},"violence":{"filtered":false,"severity":"safe"}}}],"choices":[{"text":"This is a test","index":0,"finish_reason":"length","logprobs":null,"content_filter_results":{"hate":{"filtered":false,"severity":"safe"},"self_harm":{"filtered":false,"severity":"safe"},"sexual":{"filtered":false,"severity":"safe"},"violence":{"filtered":false,"severity":"safe"}}}],"usage":{"prompt_tokens":7,"completion_tokens":50,"total_tokens":57}}\n'
    )

    with TempDirectory() as temp_dir:
        # set up simulated API in record mode
        config = _get_record_config(httpserver, temp_dir.path)
        server = UvicornTestServer(config)
        with server.run_in_thread():
            aoai_client = AzureOpenAI(
                api_key=API_KEY,
                api_version="2023-12-01-preview",
                azure_endpoint="http://localhost:8001",
                max_retries=0,
            )
            prompt = "This is a test prompt"
            response = aoai_client.completions.create(model="deployment1", prompt=prompt, max_tokens=50)

            assert len(response.choices) == 1
            assert response.choices[0].text == "This is a test"

        # Undo httpserver config to ensure there isn't an endpoint to forward to
        # when testing in replay mode
        httpserver.clear_all_handlers()

        # set up simulated API in replay mode (using the recording from above)
        config = _get_replay_config(temp_dir.path)
        server = UvicornTestServer(config)
        with server.run_in_thread():
            aoai_client = AzureOpenAI(
                api_key=API_KEY,
                api_version="2023-12-01-preview",
                azure_endpoint="http://localhost:8001",
                max_retries=0,
            )
            prompt = "This is a test prompt"
            response = aoai_client.completions.create(model="deployment1", prompt=prompt, max_tokens=50)

            assert len(response.choices) == 1
            assert response.choices[0].text == "This is a test"

            prompt = "This is a test prompt"
            response = aoai_client.completions.create(model="deployment1", prompt=prompt, max_tokens=50)

            assert len(response.choices) == 1
            assert response.choices[0].text == "This is a test"

            try:
                prompt = "This is a different prompt value and should fail as it isn't in the recording file"
                response = aoai_client.completions.create(model="deployment1", prompt=prompt, max_tokens=50)
                assert False, "Expected request to fail for non-recorded prompt"
            except InternalServerError as e:
                assert e.status_code == 500


@pytest.mark.asyncio
async def test_openai_record_replay_completion_limit_reached(httpserver: HTTPServer):
    """
    Ensure we can call the completions endpoint multiple times using recorded responses to trigger rate-limiting
    """

    # Set up pytest-httpserver to provide an endpoint for the simulator
    # to call in record mode
    httpserver.expect_request(
        uri="/openai/deployments/low_limit/completions",
        query_string="api-version=2023-12-01-preview",
        method="POST",
    ).respond_with_data(
        '{"id":"cmpl-95FbXadIqJEMZZ1Rl0chTcKRxk2ez","object":"text_completion","created":1711038651,"model":"gpt-35-turbo","prompt_filter_results":[{"prompt_index":0,"content_filter_results":{"hate":{"filtered":false,"severity":"safe"},"self_harm":{"filtered":false,"severity":"safe"},"sexual":{"filtered":false,"severity":"safe"},"violence":{"filtered":false,"severity":"safe"}}}],"choices":[{"text":"This is a test","index":0,"finish_reason":"length","logprobs":null,"content_filter_results":{"hate":{"filtered":false,"severity":"safe"},"self_harm":{"filtered":false,"severity":"safe"},"sexual":{"filtered":false,"severity":"safe"},"violence":{"filtered":false,"severity":"safe"}}}],"usage":{"prompt_tokens":7,"completion_tokens":50,"total_tokens":57}}\n'
    )

    with TempDirectory() as temp_dir:
        # set up simulated API in record mode
        config = _get_record_config(httpserver, temp_dir.path)
        server = UvicornTestServer(config)
        with server.run_in_thread():
            aoai_client = AzureOpenAI(
                api_key=API_KEY,
                api_version="2023-12-01-preview",
                azure_endpoint="http://localhost:8001",
                max_retries=0,
            )
            prompt = "This is a test prompt"
            response = aoai_client.completions.create(model="low_limit", prompt=prompt, max_tokens=50)

            assert len(response.choices) == 1
            assert response.choices[0].text == "This is a test"

        # Undo httpserver config to ensure there isn't an endpoint to forward to
        # when testing in replay mode
        httpserver.clear_all_handlers()

        # set up simulated API in replay mode (using the recording from above)
        config = _get_replay_config(temp_dir.path)
        server = UvicornTestServer(config)
        with server.run_in_thread():
            aoai_client = AzureOpenAI(
                api_key=API_KEY,
                api_version="2023-12-01-preview",
                azure_endpoint="http://localhost:8001",
                max_retries=0,
            )
            prompt = "This is a test prompt"
            response = aoai_client.completions.create(model="low_limit", prompt=prompt, max_tokens=50)

            assert len(response.choices) == 1
            assert response.choices[0].text == "This is a test"

            try:
                aoai_client.completions.create(model="low_limit", prompt=prompt, max_tokens=50)
                assert False, "Expect to be rate-limited"
            except RateLimitError as e:
                assert e.status_code == 429
                assert (
                    e.message
                    == "Error code: 429 - {'error': {'code': '429', 'message': 'Requests to the OpenAI API Simulator have exceeded call rate limit. Please retry after 60 seconds.'}}"
                )
