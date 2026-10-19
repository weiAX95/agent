"""Build the real business graph around AST-loaded functions and offline fakes."""

from types import SimpleNamespace

from src.agent.chat_workflow import ChatServices, build_chat_workflow


async def _no_op_async(*args):
    return None


def attach_chat_workflow(namespace):
    namespace["chat_workflow"] = build_chat_workflow(ChatServices(
        prepare_chat=namespace["_prepare_chat"],
        should_extract_memory=lambda message: namespace.get(
            "should_extract_memory", lambda value: False
        )(message),
        judge_memory=lambda message, callbacks=None: namespace.get(
            "judge_memory", lambda value, callbacks=None: SimpleNamespace(memories=[])
        )(message, callbacks=callbacks),
        resolve_memories=lambda items, message, callbacks=None: namespace.get(
            "resolve_memories", lambda *args, **kwargs: None
        )(items, message, callbacks=callbacks),
        validate_memory_resolutions=lambda batch, existing: namespace.get(
            "validate_memory_resolutions", lambda value, records: {}
        )(batch, existing),
        save_to_memory=SimpleNamespace(invoke=lambda values: namespace.get(
            "save_to_memory", SimpleNamespace(invoke=lambda payload: None)
        ).invoke(values)),
        replace_memory=lambda **kwargs: namespace.get(
            "replace_memory", lambda **values: None
        )(**kwargs),
        content_to_text=namespace.get("_content_to_text", lambda content: content),
        restore_stopped_turn=namespace.get("_restore_stopped_turn", _no_op_async),
        discard_incomplete_turn=namespace.get("_discard_incomplete_turn", _no_op_async),
    ))
