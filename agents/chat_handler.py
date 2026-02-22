"""agents/chat_handler.py — Background chat thread for responding while busy."""
import time, subprocess, threading
from .hub_client import hub_get, hub_msg
from .log_utils import log


def _chat_handler_loop(ctx):
    """Background thread: check for chat messages and respond while agent is busy."""
    while ctx._chat_running:
        time.sleep(2)
        if not ctx._chat_running:
            break
        try:
            chat_msgs = hub_get(ctx, f"/messages/{ctx.AGENT_NAME}/chat")
            if not chat_msgs:
                continue
            for cm in chat_msgs:
                content = cm.get("content", "")
                sender = cm.get("sender", "user")
                if not content.strip():
                    continue
                log(ctx, f"\U0001f4ac Chat from {sender}: {content[:80]}")
                task_ctx = ctx._task_context[:1000] if ctx._task_context else "No specific task context"
                chat_prompt = f"""You are {ctx.AGENT_NAME}. You are currently working on a task.
The user sent you a chat message while you are busy.

CURRENT TASK CONTEXT:
{task_ctx}

USER MESSAGE: {content}

RULES:
- Answer the user's question briefly and helpfully.
- If they're asking about your current work, explain what you're doing.
- If they want you to adjust your approach, acknowledge it and note you'll apply it.
- Do NOT use any tools. Just respond with text.
- Keep response under 3 sentences.
- Respond in the same language as the user's message."""
                try:
                    r = subprocess.run(
                        ["claude", "--model", ctx.MODEL_SONNET, "-p", chat_prompt,
                         "--output-format", "text", "--max-turns", "1"],
                        cwd=ctx.AGENT_CWD, capture_output=True, text=True, timeout=30
                    )
                    reply = r.stdout.strip() if r.returncode == 0 else "I'm busy with a task, I'll get back to you shortly."
                except Exception:
                    reply = "Working on it, I'll respond shortly."
                hub_msg(ctx, sender, reply, "chat")
                log(ctx, f"\U0001f4ac Replied: {reply}")
        except Exception:
            pass

        # Also check regular inbox for new messages from user
        try:
            peek = hub_get(ctx, f"/messages/{ctx.AGENT_NAME}?peek=true")
            current_count = peek.get("count", 0) if isinstance(peek, dict) else 0
            if current_count > ctx._inbox_count_at_task_start and not ctx._mid_task_notified:
                new_msgs = current_count - ctx._inbox_count_at_task_start
                log(ctx, f"\U0001f4ec {new_msgs} new inbox message(s) \u2014 will process after current task")
                ctx._mid_task_notified = True
        except Exception:
            pass


def start_chat_handler(ctx, task_desc=""):
    """Start the background chat handler thread."""
    ctx._task_context = task_desc
    ctx._chat_running = True
    ctx._chat_thread = threading.Thread(target=_chat_handler_loop, args=(ctx,), daemon=True)
    ctx._chat_thread.start()


def stop_chat_handler(ctx):
    """Stop the background chat handler thread."""
    ctx._chat_running = False
    if ctx._chat_thread:
        ctx._chat_thread.join(timeout=5)
        ctx._chat_thread = None
