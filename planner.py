"""Agent that interacts with OpenAPI APIs via a hierarchical planning approach."""
import json
import re
from functools import partial
from typing import Any, Callable, Dict, List, Optional
from langchain.agents import Tool
from langchain.memory import ConversationBufferMemory
from langchain.chat_models import ChatOpenAI
from langchain.utilities import SerpAPIWrapper
from langchain.agents import initialize_agent
from langchain.agents import AgentType
from getpass import getpass

import yaml
from pydantic import Field


from langchain.agents.agent import AgentExecutor
from planner_prompt import (
    API_CONTROLLER_PROMPT,
    API_CONTROLLER_TOOL_DESCRIPTION,
    API_CONTROLLER_TOOL_NAME,
    API_ORCHESTRATOR_PROMPT,
    API_PLANNER_PROMPT,
    API_PLANNER_TOOL_DESCRIPTION,
    API_PLANNER_TOOL_NAME,
    PARSING_DELETE_PROMPT,
    PARSING_GET_PROMPT,
    PARSING_PATCH_PROMPT,
    PARSING_POST_PROMPT,
    REQUESTS_DELETE_TOOL_DESCRIPTION,
    REQUESTS_GET_TOOL_DESCRIPTION,
    REQUESTS_PATCH_TOOL_DESCRIPTION,
    REQUESTS_POST_TOOL_DESCRIPTION,
)
from spec import ReducedOpenAPISpec
# from langchain.agents.mrkl.base import ZeroShotAgent
from langchain.agents.conversational_chat.base import ConversationalChatAgent
from langchain.agents.tools import Tool
from langchain.base_language import BaseLanguageModel
from langchain.callbacks.base import BaseCallbackManager
from langchain.chains.llm import LLMChain
from langchain.llms.openai import OpenAI
from langchain.memory import ReadOnlySharedMemory
from langchain.prompts import PromptTemplate
from langchain.prompts.base import BasePromptTemplate
from langchain.requests import RequestsWrapper
from langchain.tools.base import BaseTool
from langchain.tools.requests.tool import BaseRequestsTool

from langchain.agents.conversational_chat.prompt import (
    PREFIX,
    SUFFIX,
    TEMPLATE_TOOL_RESPONSE,
)
#
# Requests tools with LLM-instructed extraction of truncated responses.
#
# Of course, truncating so bluntly may lose a lot of valuable
# information in the response.
# However, the goal for now is to have only a single inference step.
MAX_RESPONSE_LENGTH = 5000


def _get_default_llm_chain(prompt: BasePromptTemplate) -> LLMChain:
    return LLMChain(
        llm=OpenAI(),
        prompt=prompt,
    )


def _get_default_llm_chain_factory(
    prompt: BasePromptTemplate,
) -> Callable[[], LLMChain]:
    """Returns a default LLMChain factory."""
    return partial(_get_default_llm_chain, prompt)


class RequestsGetToolWithParsing(BaseRequestsTool, BaseTool):
    name = "requests_get"
    description = REQUESTS_GET_TOOL_DESCRIPTION
    response_length: Optional[int] = MAX_RESPONSE_LENGTH
    llm_chain: LLMChain = Field(
        default_factory=_get_default_llm_chain_factory(PARSING_GET_PROMPT)
    )

    def _run(self, text: str) -> str:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise e
        data_params = data.get("params")
        response = self.requests_wrapper.get(data["url"], params=data_params)
        response = response[: self.response_length]
        return self.llm_chain.predict(
            response=response, instructions=data["output_instructions"]
        ).strip()

    async def _arun(self, text: str) -> str:
        raise NotImplementedError()


class RequestsPostToolWithParsing(BaseRequestsTool, BaseTool):
    name = "requests_post"
    description = REQUESTS_POST_TOOL_DESCRIPTION

    response_length: Optional[int] = MAX_RESPONSE_LENGTH
    llm_chain: LLMChain = Field(
        default_factory=_get_default_llm_chain_factory(PARSING_POST_PROMPT)
    )

    def _run(self, text: str) -> str:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            print("Error decoding JSON",text)
            raise e
        response = self.requests_wrapper.post(data["url"], data["data"])
        response = response[: self.response_length]
        return self.llm_chain.predict(
            response=response, instructions=data["output_instructions"]
        ).strip()

    async def _arun(self, text: str) -> str:
        raise NotImplementedError()


class RequestsPatchToolWithParsing(BaseRequestsTool, BaseTool):
    name = "requests_patch"
    description = REQUESTS_PATCH_TOOL_DESCRIPTION

    response_length: Optional[int] = MAX_RESPONSE_LENGTH
    llm_chain: LLMChain = Field(
        default_factory=_get_default_llm_chain_factory(PARSING_PATCH_PROMPT)
    )

    def _run(self, text: str) -> str:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise e
        response = self.requests_wrapper.patch(data["url"], data["data"])
        response = response[: self.response_length]
        return self.llm_chain.predict(
            response=response, instructions=data["output_instructions"]
        ).strip()

    async def _arun(self, text: str) -> str:
        raise NotImplementedError()


class RequestsDeleteToolWithParsing(BaseRequestsTool, BaseTool):
    name = "requests_delete"
    description = REQUESTS_DELETE_TOOL_DESCRIPTION

    response_length: Optional[int] = MAX_RESPONSE_LENGTH
    llm_chain: LLMChain = Field(
        default_factory=_get_default_llm_chain_factory(PARSING_DELETE_PROMPT)
    )

    def _run(self, text: str) -> str:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise e
        response = self.requests_wrapper.delete(data["url"])
        response = response[: self.response_length]
        return self.llm_chain.predict(
            response=response, instructions=data["output_instructions"]
        ).strip()

    async def _arun(self, text: str) -> str:
        raise NotImplementedError()


#
# Orchestrator, planner, controller.
#
def _create_api_planner_tool(
    api_spec: ReducedOpenAPISpec, llm: BaseLanguageModel, memory = None,instructions = ""
) -> Tool:
    endpoint_descriptions = [
        f"{name}" for name, description, _ in api_spec.endpoints
    ]
    prompt = PromptTemplate(
        template=instructions + API_PLANNER_PROMPT,
        input_variables=["query"],
        partial_variables={"endpoints": "- " + "- ".join(endpoint_descriptions)},
    )
    chain = LLMChain(llm=llm, prompt=prompt)
    tool = Tool(
        name=API_PLANNER_TOOL_NAME,
        description=API_PLANNER_TOOL_DESCRIPTION,
        func=chain.run,
    )
    return tool

import json
from typing import Union
from langchain.agents import AgentOutputParser
from langchain.agents.conversational_chat.prompt import FORMAT_INSTRUCTIONS
from langchain.output_parsers.json import parse_json_markdown
from langchain.schema import AgentAction, AgentFinish, OutputParserException

class ConvoOutputParser(AgentOutputParser):
    def get_format_instructions(self) -> str:
        return FORMAT_INSTRUCTIONS

    def parse(self, text: str) -> Union[AgentAction, AgentFinish]:
        try:
            response = parse_json_markdown(text)
            action, action_input = response["action"], str(response["action_input"])
            if action == "Final Answer":
                return AgentFinish({"output": action_input}, text)
            else:
                return AgentAction(action, action_input, text)
        except Exception as e:
            raise OutputParserException(f"Could not parse LLM output: {text}") from e

    @property
    def _type(self) -> str:
        return "conversational_chat"
    


def _create_api_controller_agent(
    api_url: str,
    api_docs: str,
    requests_wrapper: RequestsWrapper,
    llm: BaseLanguageModel,
    memory = None,
    instructions = "",
) -> AgentExecutor:
    get_llm_chain = LLMChain(llm=llm, prompt=PARSING_GET_PROMPT)
    post_llm_chain = LLMChain(llm=llm, prompt=PARSING_POST_PROMPT)
    tools: List[BaseTool] = [
        RequestsGetToolWithParsing(
            requests_wrapper=requests_wrapper, llm_chain=get_llm_chain
        ),
        RequestsPostToolWithParsing(
            requests_wrapper=requests_wrapper, llm_chain=post_llm_chain
        ),
    ]
    prompt = PromptTemplate(
        template=instructions + API_CONTROLLER_PROMPT,
        input_variables=[],
        partial_variables={
            "api_url": api_url,
            "api_docs": api_docs,
            # "tool_names": ", ".join([tool.name for tool in tools]),
            # "tool_descriptions": "\n".join(
            #     [f"{tool.name}: {tool.description}" for tool in tools]
            # ),
        },
    )
    agent = ConversationalChatAgent.from_llm_and_tools(
        llm,
        tools=tools,
        system_message=prompt.format(),
        human_message=SUFFIX,
        verbose=True
    )
    agent.output_parser = ConvoOutputParser()
    # agent = initialize_agent(tools, llm, agent=AgentType.CHAT_CONVERSATIONAL_REACT_DESCRIPTION, verbose=verbose, memory=memory)
    return AgentExecutor.from_agent_and_tools(
        agent=agent,
        tools=tools,
        memory=memory,
        # callback_manager=callback_manager,
        verbose=True,
    )


def _create_api_controller_tool(
    api_spec: ReducedOpenAPISpec,
    requests_wrapper: RequestsWrapper,
    llm: BaseLanguageModel,
    memory = None,
    instructions = ""
) -> Tool:
    """Expose controller as a tool.

    The tool is invoked with a plan from the planner, and dynamically
    creates a controller agent with relevant documentation only to
    constrain the context.
    """

    base_url = api_spec.servers[0]["url"]  # TODO: do better.

    def _create_and_run_api_controller_agent(plan_str: str) -> str:        
        pattern = r"\b(GET|POST|PATCH|DELETE)\s+(/\S+)*"
        matches = re.findall(pattern, plan_str)
        endpoint_names = [
            "{method} {route}".format(method=method, route=route.split("?")[0].split("]")[0])
            for method, route in matches
        ]
        endpoint_docs_by_name = {name: docs for name, _, docs in api_spec.endpoints}
        docs_str = ""
        for endpoint_name in endpoint_names:
            docs = endpoint_docs_by_name.get(endpoint_name)
            if not docs:
                raise ValueError(f"{endpoint_name} endpoint does not exist.")
            docs_str += f"== Docs for {endpoint_name} == \n{yaml.dump(docs)}\n"

        agent = _create_api_controller_agent(base_url, docs_str, requests_wrapper, llm,memory,instructions)            
        return agent.run(plan_str)

    return Tool(
        name=API_CONTROLLER_TOOL_NAME,
        func=_create_and_run_api_controller_agent,
        description=API_CONTROLLER_TOOL_DESCRIPTION,
    )


def create_openapi_agent(
    api_spec: ReducedOpenAPISpec,
    requests_wrapper: RequestsWrapper,
    llm: BaseLanguageModel,
    callback_manager: Optional[BaseCallbackManager] = None,
    verbose: bool = True,
    agent_executor_kwargs: Optional[Dict[str, Any]] = None,
    instructions: str = "",
    instructions_planner: str = "",
    instructions_controller: str = "",
    **kwargs: Dict[str, Any],
) -> AgentExecutor:
    """Instantiate API planner and controller for a given spec.

    Inject credentials via requests_wrapper.

    We use a top-level "orchestrator" agent to invoke the planner and controller,
    rather than a top-level planner
    that invokes a controller with its plan. This is to keep the planner simple.
    """
    memory = ConversationBufferMemory(memory_key="chat_history", return_messages=True)

    tools = [

        _create_api_planner_tool(api_spec, OpenAI(model_name="gpt-3.5-turbo", temperature=0.0),memory,instructions_planner),
        _create_api_controller_tool(api_spec, requests_wrapper, OpenAI(model_name="gpt-4", temperature=0.0),memory,instructions_controller),
    ]
    # prompt = PromptTemplate(
    #     template=API_ORCHESTRATOR_PROMPT,
    #     input_variables=[],
    #     partial_variables={
    #         # "tool_names": ", ".join([tool.name for tool in tools]),
    #         "tool_descriptions": "\n".join(
    #             [f"{tool.name}: {tool.description}" for tool in tools]
    #         ),
    #     },
    # )
    # print("prompt", )
    
    # agent = initialize_agent(tools, llm, agent=AgentType.CHAT_CONVERSATIONAL_REACT_DESCRIPTION, verbose=verbose, memory=memory)
    agent = ConversationalChatAgent.from_llm_and_tools(
        llm,
        tools,
        system_message= API_ORCHESTRATOR_PROMPT + "---\n" + instructions + "---\n",
        human_message=SUFFIX,
        verbose=verbose,
        **kwargs,
    )
    agent.output_parser = ConvoOutputParser()
    # return agent
    return AgentExecutor.from_agent_and_tools(
        agent=agent,
        tools=tools,
        memory=memory,
        callback_manager=callback_manager,
        verbose=verbose,
        **(agent_executor_kwargs or {}),
    )
