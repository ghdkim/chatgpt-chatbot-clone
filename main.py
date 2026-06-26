import asyncio
import base64
import json
from typing import Any

import dotenv
import streamlit as st
from openai import OpenAI
from agents import (
    Agent,
    Runner,
    SQLiteSession,
    WebSearchTool,
    FileSearchTool,
    ImageGenerationTool,
    CodeInterpreterTool,
    HostedMCPTool,
)
from agents.mcp.server import MCPServerStdio

dotenv.load_dotenv()

client = OpenAI()

VECTOR_STORE_ID = "vs_6a3e34b1e2188191bf84e0a0b76a539c"
SESSION_DB = "chat-gpt-clone-memory.db"
SESSION_ID = "chat-history"


# -----------------------------
# Streamlit page setup
# -----------------------------
st.set_page_config(
    page_title="ChatGPT Clone",
    page_icon="🤖",
    layout="wide",
)

st.title("🤖 ChatGPT Clone")
st.caption("Built with OpenAI Agents SDK, hosted tools, MCP tools, file search, image generation, and code interpreter.")


# -----------------------------
# Session setup
# -----------------------------
if "session" not in st.session_state:
    st.session_state["session"] = SQLiteSession(SESSION_ID, SESSION_DB)

session = st.session_state["session"]


# -----------------------------
# Helpers
# -----------------------------
def escape_markdown_dollars(text: Any) -> str:
    """Avoid Streamlit treating dollar signs as LaTeX delimiters."""
    if text is None:
        return ""
    return str(text).replace("$", r"\$")


def try_parse_json(value: Any) -> Any:
    """Parse JSON strings when possible; otherwise return original value."""
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return value

    value = value.strip()
    if not value:
        return value

    try:
        return json.loads(value)
    except Exception:
        return value


def render_json_or_text(value: Any, label: str | None = None) -> None:
    """Render JSON-like values nicely; fall back to text/code."""
    parsed = try_parse_json(value)

    if label:
        st.markdown(f"**{label}**")

    if isinstance(parsed, (dict, list)):
        st.json(parsed, expanded=False)
    else:
        st.code(str(parsed), language="text")


def render_mcp_tools(message: dict[str, Any]) -> None:
    """
    Render tools returned by a hosted MCP server.

    The Responses API stores listed MCP tools under message["tools"] for
    output item type "mcp_list_tools".
    """
    server_label = message.get("server_label", "MCP server")
    tools = message.get("tools", [])

    with st.chat_message("assistant"):
        with st.expander(f"🧰 {server_label}: available MCP tools ({len(tools)})", expanded=False):
            if not tools:
                st.info("No tools were returned.")
                return

            for index, tool in enumerate(tools, start=1):
                name = tool.get("name", "Unnamed tool")
                description = tool.get("description") or "No description provided."
                input_schema = tool.get("input_schema") or tool.get("parameters") or {}

                st.markdown(f"### {index}. `{name}`")
                st.write(description)

                if input_schema:
                    render_json_or_text(input_schema, "Input schema")


def render_mcp_call(message: dict[str, Any]) -> None:
    """
    Render MCP tool call details, including the output.

    Your original idea is correct:
        message["type"] == "mcp_call"
        message["server_label"]
        message["name"]
        message["arguments"]
        message["output"]

    This version uses .get() to avoid KeyErrors and renders JSON cleanly.
    """
    server_label = message.get("server_label", "MCP server")
    tool_name = message.get("name", "unknown_tool")
    arguments = message.get("arguments", {})
    output = message.get("output")
    error = message.get("error")

    with st.chat_message("assistant"):
        if error:
            st.error(f"⚒️ MCP tool failed: `{server_label}.{tool_name}`")
            render_json_or_text(error, "Error")
            return

        with st.expander(f"⚒️ MCP tool used: `{server_label}.{tool_name}`", expanded=False):
            render_json_or_text(arguments, "Arguments")
            render_json_or_text(output, "Output")


def render_message_content(content: Any) -> None:
    """Render user/assistant message content from session history."""
    if isinstance(content, str):
        st.write(escape_markdown_dollars(content))
        return

    if isinstance(content, list):
        for part in content:
            part_type = part.get("type")

            # Text content can appear under different keys depending on SDK/API version.
            if part_type in {"input_text", "output_text", "text"} or "text" in part:
                st.write(escape_markdown_dollars(part.get("text", "")))

            # Image content can appear as input_image/image_url.
            elif part_type in {"input_image", "image"} or "image_url" in part:
                image_url = part.get("image_url")
                if image_url:
                    st.image(image_url)

            else:
                render_json_or_text(part)


# -----------------------------
# Chat history renderer
# -----------------------------
async def paint_history() -> None:
    messages = await session.get_items()

    for message in messages:
        # Normal user/assistant messages
        if "role" in message:
            role = "user" if message["role"] == "user" else "assistant"

            with st.chat_message(role):
                if message.get("type") == "message" and role == "assistant":
                    render_message_content(message.get("content", ""))
                else:
                    render_message_content(message.get("content", ""))

            continue

        # Tool/event output items
        message_type = message.get("type")

        if message_type == "web_search_call":
            with st.chat_message("assistant"):
                st.caption("🔎 Used web search")

        elif message_type == "file_search_call":
            with st.chat_message("assistant"):
                st.caption("🗂️ Used file search")

        elif message_type == "image_generation_call":
            result = message.get("result")
            if result:
                image = base64.b64decode(result)
                with st.chat_message("assistant"):
                    st.image(image, caption="🖼️ Generated image")

        elif message_type == "code_interpreter_call":
            code = message.get("code")
            with st.chat_message("assistant"):
                with st.expander("💻 Code interpreter", expanded=False):
                    if code:
                        st.code(code)
                    else:
                        render_json_or_text(message)

        elif message_type == "mcp_list_tools":
            render_mcp_tools(message)

        elif message_type == "mcp_call":
            render_mcp_call(message)


def update_status(status_container, event_type: str) -> None:
    status_messages = {
        # Web Search Tool events
        "response.web_search_call.completed": ("✅ Web search completed", "complete"),
        "response.web_search_call.in_progress": ("🔎 Searching the web...", "running"),
        "response.web_search_call.searching": ("🔄 Web search in progress...", "running"),

        # File Search Tool events
        "response.file_search_call.completed": ("✅ File search completed", "complete"),
        "response.file_search_call.in_progress": ("🗂️ Searching files...", "running"),
        "response.file_search_call.searching": ("🔄 File search in progress...", "running"),

        # Image Generation Tool events
        "response.image_generation_call.completed": ("✅ Image generation completed", "complete"),
        "response.image_generation_call.in_progress": ("🖼️ Generating image...", "running"),
        "response.image_generation_call.generating": ("🔄 Image generation in progress...", "running"),

        # Code Interpreter Tool events
        "response.code_interpreter_call_code.done": ("✅ Ran code", "complete"),
        "response.code_interpreter_call_code.completed": ("✅ Code execution completed", "complete"),
        "response.code_interpreter_call_code.in_progress": ("💻 Executing code...", "running"),
        "response.code_interpreter_call_code.interpreting": ("🔄 Code execution in progress...", "running"),

        # Hosted MCP Tool events
        "response.mcp_call.completed": ("✅ MCP tool call completed", "complete"),
        "response.mcp_call.failed": ("⚠️ MCP tool call failed", "error"),
        "response.mcp_call.in_progress": ("⚒️ Calling MCP tool...", "running"),
        "response.mcp_list_tools.completed": ("✅ MCP tools loaded", "complete"),
        "response.mcp_list_tools.failed": ("⚠️ Failed to load MCP tools", "error"),
        "response.mcp_list_tools.in_progress": ("🧰 Loading MCP tools...", "running"),

        "response.completed": ("✅ Response completed", "complete"),
    }

    if event_type in status_messages:
        label, state = status_messages[event_type]
        status_container.update(label=label, state=state)


# -----------------------------
# Agent factory
# -----------------------------
def build_agent(yfinance_server: MCPServerStdio, timezone_server: MCPServerStdio) -> Agent:
    return Agent(
        name="ChatGPT Clone",
        instructions="""
You are a helpful assistant inside a ChatGPT-style Streamlit app.

Behavior:
- Answer clearly and naturally.
- Use web search for current or niche information.
- Use file search when the user asks about uploaded documents.
- Use code interpreter for calculations, data analysis, files, and charts.
- Use image generation only when the user asks to create or edit images.
- Use MCP tools when they are relevant.
- When you use external information, explain where it came from when possible.
""",
        mcp_servers=[yfinance_server, timezone_server],
        tools=[
            WebSearchTool(),
            FileSearchTool(
                vector_store_ids=[VECTOR_STORE_ID],
                max_num_results=3,
            ),
            ImageGenerationTool(
                tool_config={
                    "type": "image_generation",
                    "quality": "high",
                    "output_format": "jpeg",
                    "moderation": "low",
                    "partial_images": 1,
                }
            ),
            CodeInterpreterTool(
                tool_config={
                    "type": "code_interpreter",
                    "container": {"type": "auto"},
                }
            ),
            HostedMCPTool(
                tool_config={
                    "type": "mcp",
                    "server_label": "Context7",
                    "server_description": "Use this to get current documentation from software projects.",
                    "server_url": "https://mcp.context7.com/mcp",
                    "require_approval": "never",
                }
            ),
        ],
    )


# -----------------------------
# Agent runner
# -----------------------------
async def run_agent(user_input: str | list[dict[str, Any]]) -> None:
    yfinance_server = MCPServerStdio(
        params={
            "command": "uvx",
            "args": ["mcp-yahoo-finance"],
        },
        cache_tools_list=True,
    )

    timezone_server = MCPServerStdio(
        params={
            "command": "uvx",
            "args": ["mcp-server-time", "--local-timezone=America/New_York"],
        },
        cache_tools_list=True,
    )

    async with yfinance_server, timezone_server:
        agent = build_agent(yfinance_server, timezone_server)

        with st.chat_message("assistant"):
            status_container = st.status("⏳ Thinking...", expanded=False)
            code_placeholder = st.empty()
            image_placeholder = st.empty()
            text_placeholder = st.empty()

            st.session_state["code_placeholder"] = code_placeholder
            st.session_state["image_placeholder"] = image_placeholder
            st.session_state["text_placeholder"] = text_placeholder

            streamed_text = ""
            streamed_code = ""

            stream = Runner.run_streamed(
                agent,
                user_input,
                session=session,
            )

            async for event in stream.stream_events():
                if event.type != "raw_response_event":
                    continue

                event_type = event.data.type
                update_status(status_container, event_type)

                if event_type == "response.output_text.delta":
                    streamed_text += event.data.delta
                    text_placeholder.write(escape_markdown_dollars(streamed_text))

                elif event_type == "response.code_interpreter_call_code.delta":
                    streamed_code += event.data.delta
                    code_placeholder.code(streamed_code)

                elif event_type == "response.image_generation_call.partial_image":
                    image = base64.b64decode(event.data.partial_image_b64)
                    image_placeholder.image(image)

            status_container.update(label="✅ Done", state="complete")


# -----------------------------
# Sidebar
# -----------------------------
with st.sidebar:
    st.header("Settings")

    if st.button("Reset memory", type="secondary"):
        asyncio.run(session.clear_session())
        st.success("Memory reset. Refreshing...")
        st.rerun()

    with st.expander("Raw session items", expanded=False):
        st.json(asyncio.run(session.get_items()), expanded=False)


# -----------------------------
# Main chat UI
# -----------------------------
asyncio.run(paint_history())

prompt = st.chat_input(
    "Write a message for your assistant...",
    accept_file=True,
    file_type=["txt", "md", "csv", "json", "jpg", "jpeg", "png"],
)

if prompt:
    # Clear old streaming placeholders
    for key in ("code_placeholder", "image_placeholder", "text_placeholder"):
        if key in st.session_state:
            st.session_state[key].empty()

    user_content: list[dict[str, Any]] = []

    if prompt.text:
        user_content.append({"type": "input_text", "text": prompt.text})

    # Display user message immediately
    with st.chat_message("user"):
        if prompt.text:
            st.write(prompt.text)

        for uploaded in prompt.files:
            if uploaded.type.startswith("image/"):
                file_bytes = uploaded.getvalue()
                file_base64 = base64.b64encode(file_bytes).decode("utf-8")
                data_uri = f"data:{uploaded.type};base64,{file_base64}"

                user_content.append(
                    {
                        "type": "input_image",
                        "image_url": data_uri,
                        "detail": "auto",
                    }
                )

                st.image(data_uri, caption=uploaded.name)

    # Upload text-like files to vector store
    for uploaded in prompt.files:
        if uploaded.type.startswith("text/") or uploaded.name.lower().endswith((".txt", ".md", ".csv", ".json")):
            with st.chat_message("assistant"):
                with st.status(f"Uploading `{uploaded.name}`...", expanded=False) as status:
                    created_file = client.files.create(
                        file=(uploaded.name, uploaded.getvalue()),
                        purpose="user_data",
                    )

                    client.vector_stores.files.create(
                        vector_store_id=VECTOR_STORE_ID,
                        file_id=created_file.id,
                    )

                    status.update(label=f"✅ `{uploaded.name}` uploaded to vector store", state="complete")

    # Build input for Runner
    if user_content:
        runner_input = [{"role": "user", "content": user_content}]
    else:
        runner_input = prompt.text or "Please analyze the uploaded file."

    asyncio.run(run_agent(runner_input))
