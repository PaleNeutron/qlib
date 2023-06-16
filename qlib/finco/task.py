import os

from pathlib import Path
import io
from typing import Any, List, Union
import ruamel.yaml as yaml
import abc
import re
import subprocess
import platform

from qlib.finco.llm import APIBackend
from qlib.finco.tpl import get_tpl_path
from qlib.finco.prompt_template import PormptTemplate
from qlib.workflow.record_temp import HFSignalRecord, SignalRecord
from qlib.contrib.analyzer import HFAnalyzer, SignalAnalyzer
from qlib.utils import init_instance_by_config
from qlib.workflow import R
from qlib.finco.log import FinCoLog, LogColors
from qlib.finco.conf import Config

COMPONENT_LIST = ["Dataset", "DataHandler", "Model", "Record", "Strategy", "Backtest"]


class Task:
    """
    The user's intention, which was initially represented by a prompt, is achieved through a sequence of tasks.
    This class doesn't have to be abstract, but it is abstract in the sense that it is not supposed to be instantiated directly because it doesn't have any implementation.

    Some thoughts:
    - Do we have to split create a new concept of Action besides Task?
        - Most actions directly modify the disk, with their interfaces taking in and outputting text. The LLM's interface similarly takes in and outputs text.
        - Some actions will run some commands.

    Maybe we can just categorizing tasks by following?
    - Planning task (it is at a high level and difficult to execute directly; therefore, it should be further divided):
    - Action Task
        - CMD Task: it is expected to run a cmd
        - Edit Task: it is supposed to edit the code base directly.
    """

    def __init__(self) -> None:
        self._context_manager = None
        self.prompt_template = PormptTemplate()
        self.executed = False
        self.continuous = Config().continuous_mode
        self.logger = FinCoLog()

    def summarize(self) -> str:
        """After the execution of the task, it is supposed to generated some context about the execution"""
        """This function might be converted to abstract method in the future"""
        self.logger.info(f"{self.__class__.__name__}: The task has nothing to summarize", plain=True)

    def assign_context_manager(self, context_manager):
        """assign the workflow context manager to the task"""
        """then all tasks can use this context manager to share the same context"""
        self._context_manager = context_manager

    def save_chat_history_to_context_manager(self, user_input, response, system_prompt):
        chat_history = self._context_manager.get_context("chat_history")
        if chat_history is None:
            chat_history = {}
        if self.__class__.__name__ not in chat_history:
            chat_history[self.__class__.__name__] = []
        chat_history[self.__class__.__name__].append({"role": "system", "content": system_prompt})
        chat_history[self.__class__.__name__].append({"role": "user", "content": user_input})
        chat_history[self.__class__.__name__].append({"role": "assistant", "content": response})
        self._context_manager.update_context("chat_history", chat_history)

    @abc.abstractclassmethod
    def execute(self, **kwargs) -> Any:
        """The execution results of the task"""
        """All sub classes should implement the execute method to determine the next task"""
        raise NotImplementedError

    def interact(self, prompt: str, **kwargs) -> Any:
        """
            The user can interact with the task. This method only handle business in current task. It will return True
            while continuous is True. This method will return user input if input cannot be parsed as 'yes' or 'no'.
            @return True, False, str
        """
        self.logger.info(title="Interact")
        if self.continuous:
            return True

        try:
            answer = input(prompt)
        except KeyboardInterrupt:
            self.logger.info("User has exited the program.")
            exit()

        if answer.lower().strip() in ["y", "yes"]:
            return True
        elif answer.lower().strip() in ["n", "no"]:
            return False
        else:
            return answer

    @property
    def system(self):
        return self.prompt_template.__getattribute__(
            self.__class__.__name__ + "_system"
        )

    @property
    def user(self):
        return self.prompt_template.__getattribute__(self.__class__.__name__ + "_user")

    def __str__(self):
        return self.__class__.__name__


class WorkflowTask(Task):
    """This task is supposed to be the first task of the workflow"""

    def __init__(self) -> None:
        super().__init__()

    def execute(self) -> List[Task]:
        """make the choice which main workflow (RL, SL) will be used"""
        user_prompt = self._context_manager.get_context("user_prompt")
        prompt_workflow_selection = self.user.render(user_prompt=user_prompt)
        response = APIBackend().build_messages_and_create_chat_completion(
            prompt_workflow_selection, self.system.render()
        )
        self.save_chat_history_to_context_manager(
            prompt_workflow_selection, response, self.system.render()
        )
        workflow = response.split(":")[1].strip().lower()
        self.executed = True
        self._context_manager.set_context("workflow", workflow)

        confirm = self.interact(
            f"The workflow has been determined to be: "
            f"{LogColors().render(workflow, color=LogColors.YELLOW, style=LogColors.BOLD)}\n"
            f"Enter 'y' to authorise command,'s' to run self-feedback commands, "
            f"'n' to exit program, or enter feedback for WorkflowTask: "
        )
        if confirm is False:
            return []

        if workflow == "supervised learning":
            return [SLPlanTask()]
        elif workflow == "reinforcement learning":
            return [RLPlanTask()]
        else:
            raise ValueError(f"The workflow: {workflow} is not supported")


class PlanTask(Task):
    pass


class SLPlanTask(PlanTask):
    def __init__(self,) -> None:
        super().__init__()

    def execute(self):
        workflow = self._context_manager.get_context("workflow")
        assert (workflow == "supervised learning"), "The workflow is not supervised learning"

        user_prompt = self._context_manager.get_context("user_prompt")
        assert user_prompt is not None, "The user prompt is not provided"
        prompt_plan_all = self.user.render(user_prompt=user_prompt)
        response = APIBackend().build_messages_and_create_chat_completion(
            prompt_plan_all, self.system.render()
        )
        self.save_chat_history_to_context_manager(
            prompt_plan_all, response, self.system.render()
        )
        if "components" not in response:
            self.logger.warning(
                "The response is not in the correct format, which probably means the answer is not correct"
            )

        regex_dict = {
            "Dataset": re.compile("Dataset: \((.*?)\) (.*?)\n"),
            "DataHandler": re.compile("DataHandler: \((.*?)\) (.*?)\n"),
            "Model": re.compile("Model: \((.*?)\) (.*?)\n"),
            "Record": re.compile("Record: \((.*?)\) (.*?)\n"),
            "Strategy": re.compile("Strategy: \((.*?)\) (.*?)\n"),
            "Backtest": re.compile("Backtest: \((.*?)\) (.*?)$"),
        }
        new_task = []
        # 1) create a workspace
        # TODO: we have to make choice between `sl` and  `sl-cfg`
        new_task.append(
            CMDTask(
                cmd_intention=f"Copy folder from {get_tpl_path() / 'sl'} to {self._context_manager.get_context('workspace')}"
            )
        )

        # 2) CURD on the workspace
        for name, regex in regex_dict.items():
            res = re.search(regex, response)
            if not res:
                self.logger.error(f"The search for {name} decision failed")
            else:
                self._context_manager.set_context(f"{name}_decision", res.group(1))
                self._context_manager.set_context(f"{name}_plan", res.group(2))
                assert res.group(1) in ["Default", "Personized"]
                if res.group(1) == "Default":
                    new_task.extend([ConfigActionTask(name), YamlEditTask(name)])
                elif res.group(1) == "Personized":
                    new_task.extend([ConfigActionTask(name), ImplementActionTask(name), YamlEditTask(name), CodeDumpTask(name)])
        return new_task


class RLPlanTask(PlanTask):
    def __init__(
            self,
    ) -> None:
        super().__init__()
        self.logger.error("The RL task is not implemented yet")
        exit()

    def execute(self):
        """
        return a list of interested tasks
        Copy the template project maybe a part of the task
        """
        return []


class RecorderTask(Task):
    """
    This Recorder task is responsible for analysing data such as index and distribution.
    """

    def __init__(self):
        super().__init__()

    def execute(self):
        workflow_config = (
            self._context_manager.get_context("workflow_config")
            if self._context_manager.get_context("workflow_config")
            else "workflow_config.yaml"
        )
        workspace = self._context_manager.get_context("workspace")
        workflow_path = workspace.joinpath(workflow_config)
        with workflow_path.open() as f:
            workflow = yaml.safe_load(f)

        confirm = self.interact(f"I select this workflow file: "
                                f"{LogColors().render(workflow_path, color=LogColors.YELLOW, style=LogColors.BOLD)}\n"
                                f"{yaml.dump(workflow, default_flow_style=False)}"
                                f"Are you sure you want to use? yes(Y/y), no(N/n):")
        if confirm is False:
            return []

        model = init_instance_by_config(workflow["task"]["model"])
        dataset = init_instance_by_config(workflow["task"]["dataset"])

        with R.start(experiment_name="finCo"):
            model.fit(dataset)
            R.save_objects(trained_model=model)

            # prediction
            recorder = R.get_recorder()
            sr = SignalRecord(model, dataset, recorder)
            sr.generate()

        self._context_manager.set_context("model", model)
        self._context_manager.set_context("dataset", dataset)
        self._context_manager.set_context("recorder", recorder)

        return [AnalysisTask()]


class AnalysisTask(Task):
    """
    This Recorder task is responsible for analysing data such as index and distribution.
    """

    __ANALYZERS_PROJECT = {
        HFAnalyzer.__name__: HFSignalRecord,
        SignalAnalyzer.__name__: SignalRecord,
    }
    __ANALYZERS_DOCS = {
        HFAnalyzer.__name__: HFAnalyzer.__doc__,
        SignalAnalyzer.__name__: SignalAnalyzer.__doc__,
    }

    def __init__(self):
        super().__init__()

    def execute(self):
        prompt = self.user.render(
            user_prompt=self._context_manager.get_context("user_prompt")
        )
        be = APIBackend()
        be.debug_mode = False

        while True:
            response = be.build_messages_and_create_chat_completion(
                prompt,
                self.system.render(
                    ANALYZERS_list=list(self.__ANALYZERS_DOCS.keys()),
                    ANALYZERS_DOCS=self.__ANALYZERS_DOCS,
                ),
            )
            analysers = response.split(",")
            confirm = self.interact(f"I select these analysers: {analysers}\n"
                                    f"Are you sure you want to use? yes(Y/y), no(N/n) or prompt:")
            if confirm is False:
                analysers = []
                break
            elif confirm is True:
                break
            else:
                prompt = confirm

        if isinstance(analysers, list) and len(analysers):
            self.logger.info(f"selected analysers: {analysers}", plain=True)

            tasks = []
            for analyser in analysers:
                if analyser in self.__ANALYZERS_PROJECT.keys():
                    tasks.append(
                        self.__ANALYZERS_PROJECT.get(analyser)(
                            workspace=self._context_manager.get_context("workspace"),
                            model=self._context_manager.get_context("model"),
                            dataset=self._context_manager.get_context("dataset"),
                            recorder=self._context_manager.get_context("recorder"),
                        )
                    )

            for task in tasks:
                resp = task.analyse()
                self._context_manager.set_context(task.__class__.__name__, resp)

        return []


class ActionTask(Task):
    pass


class CMDTask(ActionTask):
    """
    This CMD task is responsible for ensuring compatibility across different operating systems.
    """

    def __init__(self, cmd_intention: str, cwd=None):
        self.cwd = cwd
        self.cmd_intention = cmd_intention
        self._output = None
        super().__init__()

    def execute(self):
        prompt = self.user.render(
            cmd_intention=self.cmd_intention, user_os=platform.system()
        )
        response = APIBackend().build_messages_and_create_chat_completion(
            prompt, self.system.render()
        )
        self._output = subprocess.check_output(response, shell=True, cwd=self.cwd)
        return []

    def summarize(self):
        if self._output is not None:
            # TODO: it will be overrides by later commands
            # utf8 can't decode normally on Windows
            self._context_manager.set_context(
                self.__class__.__name__, self._output.decode("ANSI")
            )


class DifferentiatedComponentActionTask(ActionTask):
    @property
    def system(self):
        return self.prompt_template.__getattribute__(self.__class__.__name__ + "_system_" + self.target_component)

    @property
    def user(self):
        return self.prompt_template.__getattribute__(self.__class__.__name__ + "_user_" + self.target_component)


class ConfigActionTask(DifferentiatedComponentActionTask):
    def __init__(self, component) -> None:
        super().__init__()
        self.target_component = component

    def execute(self):
        user_prompt = self._context_manager.get_context("user_prompt")
        prompt_element_dict = dict()
        for component in COMPONENT_LIST:
            prompt_element_dict[
                f"{component}_decision"
            ] = self._context_manager.get_context(f"{component}_decision")
            prompt_element_dict[
                f"{component}_plan"
            ] = self._context_manager.get_context(f"{component}_plan")

        assert (
                None not in prompt_element_dict.values()
        ), "Some decision or plan is not set by plan maker"

        config_prompt = self.user.render(
            user_requirement=user_prompt,
            decision=prompt_element_dict[f"{self.target_component}_decision"],
            plan=prompt_element_dict[f"{self.target_component}_plan"],
        )
        response = APIBackend().build_messages_and_create_chat_completion(
            config_prompt, self.system.render()
        )
        self.save_chat_history_to_context_manager(
            config_prompt, response, self.system.render()
        )
        res = re.search(
            r"Config:(.*)Reason:(.*)Improve suggestion:(.*)", response, re.S
        )
        assert (
            res is not None and len(res.groups()) == 3
        ), "The response of config action task is not in the correct format"

        config = re.search(r"```yaml(.*)```", res.group(1), re.S)
        assert (
            config is not None
        ), "The config part of config action task response is not in the correct format"
        config = config.group(1)
        reason = res.group(2)
        improve_suggestion = res.group(3)

        self._context_manager.set_context(f"{self.target_component}_config", config)
        self._context_manager.set_context(f"{self.target_component}_reason", reason)
        self._context_manager.set_context(
            f"{self.target_component}_improve_suggestion", improve_suggestion
        )
        return []


class ImplementActionTask(DifferentiatedComponentActionTask):
    def __init__(self, target_component) -> None:
        super().__init__()
        self.target_component = target_component
        assert COMPONENT_LIST.index(self.target_component) <= 2, "The target component is not in dataset datahandler and model"

    def execute(self):
        """
        return a list of interested tasks
        Copy the template project maybe a part of the task
        """

        user_prompt = self._context_manager.get_context("user_prompt")
        prompt_element_dict = dict()
        for component in COMPONENT_LIST:
            prompt_element_dict[
                f"{component}_decision"
            ] = self._context_manager.get_context(f"{component}_decision")
            prompt_element_dict[
                f"{component}_plan"
            ] = self._context_manager.get_context(f"{component}_plan")

        assert (
            None not in prompt_element_dict.values()
        ), "Some decision or plan is not set by plan maker"
        config = self._context_manager.get_context(f"{self.target_component}_config")

        implement_prompt = self.user.render(
            user_requirement=user_prompt,
            decision=prompt_element_dict[f"{self.target_component}_decision"],
            plan=prompt_element_dict[f"{self.target_component}_plan"],
            user_config=config,
        )
        response = APIBackend().build_messages_and_create_chat_completion(
            implement_prompt, self.system.render()
        )
        self.save_chat_history_to_context_manager(
            implement_prompt, response, self.system.render()
        )

        res = re.search(
            r"Code:(.*)Explanation:(.*)Modified config:(.*)", response, re.S
        )
        assert (
            res is not None and len(res.groups()) == 3
        ), f"The response of implement action task of component {self.target_component} is not in the correct format"

        code = re.search(r"```python(.*)```", res.group(1), re.S)
        assert (
            code is not None
        ), "The code part of implementation action task response is not in the correct format"
        code = code.group(1)
        explanation = res.group(2)
        modified_config = re.search(r"```yaml(.*)```", res.group(3), re.S)
        assert (
            modified_config is not None
        ), "The modified config part of implementation action task response is not in the correct format"
        modified_config = modified_config.group(1)

        self._context_manager.set_context(f"{self.target_component}_code", code)
        self._context_manager.set_context(
            f"{self.target_component}_code_explanation", explanation
        )
        self._context_manager.set_context(
            f"{self.target_component}_modified_config", modified_config
        )

        return []


class YamlEditTask(ActionTask):
    """This yaml edit task will replace a specific component directly"""

    def __init__(self, target_component: str):
        """

        Parameters
        ----------
        file
            a target file that needs to be modified
        module_path
            the path to the section that needs to be replaced with `updated_content`
        updated_content
            The content to replace the original content in `module_path`
        """
        super().__init__()
        self.target_component = target_component
        self.target_config_key = {
            "Dataset": "dataset",
            "DataHandler": "handler",
            "Model": "model",
            "Strategy": "strategy",
            "Record": "record",
            "Backtest": "backtest",
        }[self.target_component]
    
    def replace_key_value_recursive(self, target_dict, target_key, new_value):
        for key, value in target_dict.items():
            if key == target_key:
                target_dict[key] = new_value
                return True
            elif isinstance(value, dict):
                replace_result = self.replace_key_value_recursive(value, target_key, new_value)
                if replace_result:
                    return replace_result
        return False


    def execute(self):
        # 1) read original and new content
        self.original_config_location = Path(os.path.join(self._context_manager.get_context('workspace'), "workflow_config.yaml"))
        with self.original_config_location.open("r") as f:
            target_config = yaml.safe_load(f)
        update_config_string = self._context_manager.get_context(f'{self.target_component}_modified_config')
        if update_config_string is None:
            update_config_string = self._context_manager.get_context(f'{self.target_component}_config')
        update_config = yaml.safe_load(io.StringIO(update_config_string))

        # 2) replace the module and save
        assert isinstance(update_config, dict) and self.target_config_key in update_config, "The config file is not in the correct format"
        assert self.replace_key_value_recursive(target_config, self.target_config_key, update_config[self.target_config_key]), "Replace of the yaml file failed."
        with self.original_config_location.open("w") as f:
            yaml.dump(target_config, f)

        return []

class CodeDumpTask(ActionTask):
    def __init__(self, target_component) -> None:
        super().__init__()
        self.target_component = target_component
    
    def execute(self):
        code = self._context_manager.get_context(f'{self.target_component}_code')
        assert code is not None, "The code is not set"
        
        with open(os.path.join(self._context_manager.get_context('workspace'), f'{self.target_component}_code.py'), 'w') as f:
            f.write(code)
        
        try:
            exec(f"from qlib.finco.{os.path.basename(self._context_manager.get_context('workspace'))}.{self.target_component}_code import *")
        except ImportError:
            return [ImplementActionTask(self.target_component), CodeDumpTask(self.target_component)]

class SummarizeTask(Task):
    __DEFAULT_WORKSPACE = "./"

    __DEFAULT_USER_PROMPT = (
        "Summarize the information I offered and give me some advice."
    )

    # TODO: 2048 is close to exceed GPT token limit
    __MAX_LENGTH_OF_FILE = 2048
    __DEFAULT_REPORT_NAME = "finCoReport.md"

    def __init__(self):
        super().__init__()
        self.workspace = self.__DEFAULT_WORKSPACE

    def execute(self) -> Any:
        workspace = self._context_manager.get_context("workspace")
        if workspace is not None:
            self.workspace = workspace

        user_prompt = self._context_manager.get_context("user_prompt")
        user_prompt = (
            user_prompt if user_prompt is not None else self.__DEFAULT_USER_PROMPT
        )

        file_info = self.get_info_from_file(workspace)
        context_info = []  # too long context make response unstable.
        figure_path = self.get_figure_path()

        information = context_info + file_info
        prompt_workflow_selection = self.user.render(
            information=information, figure_path=figure_path, user_prompt=user_prompt
        )

        be = APIBackend()
        be.debug_mode = False
        response = be.build_messages_and_create_chat_completion(
            user_prompt=prompt_workflow_selection, system_prompt=self.system.render()
        )
        self.save_markdown(content=response)
        self.logger.info(f"Report has saved to {self.__DEFAULT_REPORT_NAME}", title="End")

        return []

    def summarize(self) -> str:
        return ""

    def interact(self) -> Any:
        return

    def get_info_from_file(self, path) -> List:
        """
        read specific type of files under path
        """
        file_list = []
        path = Path.cwd().joinpath(path).resolve()
        for root, dirs, files in os.walk(path):
            for filename in files:
                file_path = os.path.join(root, filename)
                file_list.append(file_path)

        result = []
        for file in file_list:
            postfix = file.split(".")[-1]
            if postfix in ["py", "log", "yaml"]:
                with open(file) as f:
                    content = f.read()
                    self.logger.info(f"file to summarize: {file}", plain=True)
                    # in case of too large file
                    # TODO: Perhaps summarization method instead of truncation would be a better approach
                    result.append(
                        {"file": file, "content": content[: self.__MAX_LENGTH_OF_FILE]}
                    )

        return result

    def get_info_from_context(self):
        context = []
        # TODO: get all keys from context?
        for key in [
            "user_prompt",
            "chat_history",
            "Dataset_plan",
            "Model_plan",
            "Record_plan",
            "Strategy_plan",
            "Backtest_plan",
        ]:
            c = self._context_manager.get_context(key=key)
            if c is not None:
                c = str(c)
                context.append({key: c[: self.__MAX_LENGTH_OF_FILE]})
        return context

    def get_figure_path(self):
        file_list = []

        for root, dirs, files in os.walk(Path(self.workspace)):
            for filename in files:
                postfix = filename.split(".")[-1]
                if postfix in ["jpeg"]:
                    file_list.append(str(Path(self.workspace).joinpath(filename)))
        return file_list

    def save_markdown(self, content: str):
        with open(Path(self.workspace).joinpath(self.__DEFAULT_REPORT_NAME), "w") as f:
            f.write(content)
