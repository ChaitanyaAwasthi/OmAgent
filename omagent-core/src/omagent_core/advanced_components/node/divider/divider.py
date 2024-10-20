import json
import re
from pathlib import Path
from typing import List, Tuple

from pydantic import Field
from tenacity import (
    retry,
    retry_if_exception_message,
    stop_after_attempt,
    stop_after_delay,
)

from ....memories.ltms.ltm import LTM
from ....utils.env import EnvVar
from ....utils.registry import registry
from ....models.llms.base import BaseLLMBackend
from ....models.llms.prompt.prompt import PromptTemplate
from ....tool_system.manager import ToolManager
from ....engine.node.decider import BaseDecider
from ....engine.workflow.context import BaseWorkflowContext
from ....engine.task.agent_task import AgentTask

CURRENT_PATH = Path(__file__).parents[0]


@registry.register_node()
class TaskDivider(BaseLLMBackend, BaseDecider):
    prompts: List[PromptTemplate] = Field(
        default=[
            PromptTemplate.from_file(
                CURRENT_PATH.joinpath("sys_prompt.prompt"), role="system"
            ),
            PromptTemplate.from_file(
                CURRENT_PATH.joinpath("user_prompt.prompt"), role="user"
            ),
        ]
    )
    tool_manager: ToolManager

    @retry(
        stop=(
            stop_after_delay(EnvVar.STOP_AFTER_DELAY)
            | stop_after_attempt(EnvVar.STOP_AFTER_ATTEMPT)
        ),
        retry=retry_if_exception_message(message="LLM generation is not valid."),
        reraise=True,
    )
    def _run(self, args: BaseWorkflowContext, ltm: LTM) -> Tuple[BaseWorkflowContext, str]:
        task: AgentTask = args.task
        if task.task_depth() >= EnvVar.MAX_TASK_DEPTH:
            args.last_output = "failed: Max subtask depth reached"
            divide_failed_structure = {
                "parent_task": task.task,
                "failed_reason": "Max subtask depth reached",
            }
            self.callback.send_block(divide_failed_structure)
            return args, "failed"

        chat_complete_res = self.simple_infer(
            parent_task=task.task,
            uplevel_tasks=task.parent.sibling_info() if task.parent else [],
            former_results=args.last_output,
            tools=self.tool_manager.generate_prompt(),
        )
        chat_complete_res = self._extract_from_result(chat_complete_res)
        if chat_complete_res.get("tasks"):
            task.add_subtasks(chat_complete_res["tasks"])
            divided_detail_structure = {
                "parent_task": task.task,
                "children_tasks": [
                    {
                        f"subtask_{idx}": child.task,
                        f"subtask_{idx} milestones": " & ".join(child.milestones),
                    }
                    for idx, child in enumerate(task.children)
                ],
            }
            self.callback.send_block(divided_detail_structure)
            return args, "success"

        elif chat_complete_res.get("failed_reason"):
            args.last_output = (
                "failed: Subtask generation failed. Agent provided reason: {}".format(
                    chat_complete_res.get("failed_reason", "No reason generated.")
                )
            )
            return args, "failed"
        else:
            raise ValueError("LLM generation is not valid.")

    @retry(
        stop=(
            stop_after_delay(EnvVar.STOP_AFTER_DELAY)
            | stop_after_attempt(EnvVar.STOP_AFTER_ATTEMPT)
        ),
        retry=retry_if_exception_message(message="LLM generation is not valid."),
        reraise=True,
    )
    async def _arun(self, args: BaseWorkflowContext, ltm: LTM) -> Tuple[BaseWorkflowContext, str]:
        task: AgentTask = args.task
        if task.task_depth() >= EnvVar.MAX_TASK_DEPTH:
            args.last_output = "failed: Max subtask depth reached"
            return args, "failed"

        chat_complete_res = await self.simple_ainfer(
            parent_task=task.task,
            uplevel_tasks=task.sibling_info(),
            former_results=args.last_output,
        )
        chat_complete_res = self._extract_from_result(chat_complete_res)
        if chat_complete_res.get("tasks"):
            task.add_subtasks(chat_complete_res["tasks"])
            return args, "success"

        elif chat_complete_res.get("failed_reason"):
            args.last_output = (
                "failed: Subtask generation failed. Agent provided reason: {}".format(
                    chat_complete_res.get("failed_reason", "No reason generated.")
                )
            )
            return args, "failed"
        else:
            raise ValueError("LLM generation is not valid.")

    def _extract_from_result(self, result: str) -> dict:
        try:
            string = result["choices"][0]["message"]["content"]
            pattern = r"```json\s+(.*?)\s+```"
            match = re.search(pattern, string, re.DOTALL)
            if match:
                return json.loads(match.group(1))
            else:
                return json.loads(string)
        except Exception as error:
            raise ValueError("LLM generation is not valid.")
