"""Keep durable WeChat follow-up work on its independent model selection."""
from config.wechat_model import WECHAT_MODEL


def background_model_config(config, source_message):
    # Read durable source provenance, so retries after restart keep the same
    # channel choice without changing shared browser configuration.
    if source_message and source_message.get("origin") == "wechat":
        return {**config, "LLM_MODEL": WECHAT_MODEL}
    return config
