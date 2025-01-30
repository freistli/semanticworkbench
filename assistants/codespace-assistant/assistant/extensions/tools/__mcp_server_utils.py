import logging
from contextlib import AsyncExitStack, asynccontextmanager
from typing import AsyncIterator, List, Optional

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client

from .__mcp_server_configs import get_mcp_server_configs
from .__model import MCPServerConfig, MCPSession, ToolsConfigModel

logger = logging.getLogger(__name__)


@asynccontextmanager
async def connect_to_mcp_server(server_config: MCPServerConfig) -> AsyncIterator[Optional[ClientSession]]:
    """Connect to a single MCP server defined in the config."""
    if server_config.command.startswith("http"):
        async with connect_to_mcp_server_sse(server_config) as client_session:
            yield client_session
    else:
        async with connect_to_mcp_server_stdio(server_config) as client_session:
            yield client_session


@asynccontextmanager
async def connect_to_mcp_server_stdio(server_config: MCPServerConfig) -> AsyncIterator[Optional[ClientSession]]:
    """Connect to a single MCP server defined in the config."""

    server_params = StdioServerParameters(command=server_config.command, args=server_config.args, env=server_config.env)
    try:
        logger.debug(
            f"Attempting to connect to {server_config.name} with command: {server_config.command} {' '.join(server_config.args)}"
        )
        async with stdio_client(server_params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client_session:
                await client_session.initialize()
                yield client_session  # Yield the session for use
    except Exception as e:
        logger.exception(f"Error connecting to {server_config.name}: {e}")
        yield None  # Yield None if connection fails


@asynccontextmanager
async def connect_to_mcp_server_sse(server_config: MCPServerConfig) -> AsyncIterator[Optional[ClientSession]]:
    """Connect to a single MCP server defined in the config using SSE transport."""

    try:
        logger.debug(f"Attempting to connect to {server_config.name} with SSE transport: {server_config.command}")
        async with sse_client(url=server_config.command, headers=server_config.env) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client_session:
                await client_session.initialize()
                yield client_session  # Yield the session for use
    except Exception as e:
        logger.exception(f"Error connecting to {server_config.name}: {e}")
        yield None


async def establish_mcp_sessions(tools_config: ToolsConfigModel, stack: AsyncExitStack) -> List[MCPSession]:
    """
    Establish connections to MCP servers using the provided AsyncExitStack.
    """

    mcp_sessions: List[MCPSession] = []
    for server_config in get_mcp_server_configs(tools_config):
        client_session: ClientSession | None = await stack.enter_async_context(connect_to_mcp_server(server_config))
        if client_session:
            # Create an MCP session with the client session
            mcp_session = MCPSession(name=server_config.name, client_session=client_session)
            # Initialize the session to load tools, resources, etc.
            await mcp_session.initialize()
            # Add the session to the list of established sessions
            mcp_sessions.append(mcp_session)
        else:
            logger.warning(f"Could not establish session with {server_config.name}")
    return mcp_sessions


def get_mcp_server_prompts(tools_config: ToolsConfigModel) -> List[str]:
    """Get the prompts for all MCP servers."""
    return [server.prompt for server in get_mcp_server_configs(tools_config) if server.prompt]
