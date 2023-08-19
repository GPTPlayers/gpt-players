from typing import Any, List, Dict, Union
import inspect
import re
import json
import openai

from .util import print_in_color

WORLD_PARAM_NAME = 'self'
AGENT_PARAM_NAME = 'agent'
AGENT_NAME_PARAM_NAME = 'agent_name'

CALLABLE_FUNCTION_TABLE = dict()


class agent_callable:
    """
    A decorator that registers a function as a callable for agent to call.

    Args:
        role_name_filter: a regex string to filter the role name of agent. 
            Default to '.*'.
    """

    def __init__(self, role_name_filter: str = '.*'):
        self.role_name_filter = role_name_filter

    def __call__(self, function: callable):
        global CALLABLE_FUNCTION_TABLE

        # Get the parameters of the function
        signature = inspect.signature(function)
        parameters = signature.parameters
        has_world_param = WORLD_PARAM_NAME in parameters
        has_agent_param = AGENT_PARAM_NAME in parameters
        has_agent_name_param = AGENT_NAME_PARAM_NAME in parameters
        parameters_clean = {
            name: parameter for name, parameter in parameters.items()
            if name not in {WORLD_PARAM_NAME, AGENT_PARAM_NAME, AGENT_NAME_PARAM_NAME}}

        required_parameters = []
        for name, parameter in parameters_clean.items():
            if parameter.default is inspect.Parameter.empty:
                required_parameters.append(name)

        # Get the type of each parameter by parsing the signature
        json_schema_types = dict()
        type_annotation_to_json_schema_type = {
            str: 'string',
            int: 'integer',
            float: 'number',
            bool: 'boolean',
        }
        for name, parameter in parameters_clean.items():
            if parameter.annotation is not inspect.Parameter.empty:
                type_annotation = parameter.annotation
                json_schema_types[name] = type_annotation_to_json_schema_type.get(
                    type_annotation, 'string')

        # Get the description of each parameter by parsing the doc
        doc = function.__doc__
        if doc is None:
            doc = ''
        parameter_descriptions = {}
        for name in parameters_clean:
            pattern = rf'{name}:(.*)'
            match = re.search(pattern, doc)
            if match:
                parameter_descriptions[name] = match.group(1).strip()

        # Get function description from function doc string
        if 'Args:' in doc:
            function_description = doc.split('Args:')[0].strip()
        elif 'Returns:' in doc:
            function_description = doc.split('Returns:')[0].strip()
        else:
            function_description = doc.strip()
        function_description = '\n'.join(
            [line.strip() for line in function_description.split('\n') if len(line.strip()) > 0])

        # Register the function
        func_sig = {
            'name': function.__name__,
            'description': function_description,
            'parameters': {
                'type': 'object',
                'properties': {
                    name: {
                        'type': json_schema_types.get(name, 'string'),
                        'description': parameter_descriptions.get(name, ''),
                    } for name in parameters_clean
                },
                'required': required_parameters,
            },
        }
        CALLABLE_FUNCTION_TABLE[function.__name__] = {
            'sig': func_sig,
            'function': function,
            'has_world_param': has_world_param,
            'has_agent_param': has_agent_param,
            'has_agent_name_param': has_agent_name_param,
            'role_name_filter': self.role_name_filter,
        }
        return function


def stream_chat_completion(engine: str, messages: List[dict], print_output: bool = True, **kwargs):
    resp = openai.ChatCompletion.create(
        model=engine,
        messages=messages,
        stream=True,
        **kwargs
    )
    role = ''
    content = ''
    function_call = dict()
    for chunk in resp:
        for c in chunk['choices']:
            delta = c['delta']
            if 'role' in delta:
                role = delta['role']

            if 'function_call' in delta:
                for key, val in delta['function_call'].items():
                    if key not in function_call:
                        function_call[key] = val
                    else:
                        function_call[key] += val

            if 'content' in delta:
                if len(content) == 0 and delta['content'] == '\n\n' or delta['content'] is None:
                    continue
                content += delta['content']
                if print_output:
                    print_in_color(delta['content'], 'yellow', end='')

    if len(content) > 0 and print_output:
        print()

    message = dict()
    message['role'] = role
    message['content'] = content
    if len(function_call) > 0:
        message['function_call'] = function_call
    return message


DEFAULT_FUNCTION_CALL_REPEATS = 10
DEFAULT_IGNORE_NONE_FUNCTION_MESSAGES = True


class Agent:
    """ An agent that can think and act.

    Args:
        name (str): The name of the agent.
        prompt (str): The prompt to start the agent with.
        role (str, optional): The role of the agent. Defaults to 'agent'.
        engine (str, optional): The GPT engine to use. Defaults to 'gpt-3.5-turbo-16k'.
        function_call_repeats (int, optional): The number of times to repeat function calls in agent.think_and_act().
        ignore_none_function_messages (bool, optional): Whether to ignore messages that does not involve function calling.
    """
    name: str
    role: str = 'agent'
    memory: List[dict] = []
    engine: str = 'gpt-3.5-turbo-16k'
    engine_args: dict = dict(temperature=1.0)
    function_call_repeats: int = 1
    ignore_none_function_messages: bool = True

    def __init__(self, name: str, prompt: str,
                 engine: str = 'gpt-3.5-turbo-16k', role: str = 'agent',
                 function_call_repeats: int = DEFAULT_FUNCTION_CALL_REPEATS,
                 ignore_none_function_messages: bool = DEFAULT_IGNORE_NONE_FUNCTION_MESSAGES):
        self.name = name
        self.engine = engine
        self.role = role
        self.memory = [
            {"role": "system",  "content": prompt},
        ]
        self.function_call_repeats = function_call_repeats
        self.ignore_none_function_messages = ignore_none_function_messages

    def print_memory(self):
        """ Print the agent's memory. """
        for idx, message in enumerate(self.memory):
            print_in_color(
                f'    [{idx}] {message["role"]}: {message["content"]}', 'green')

    def _callable_function_descriptions(self):
        """
        Get the descriptions of all GPT callable functions.
        """
        ds = []
        for _, function in CALLABLE_FUNCTION_TABLE.items():
            role_name_filter = function['role_name_filter']
            # use regex to match role name filter
            if not re.match(role_name_filter, self.role):
                continue
            ds.append(function['sig'])
        return ds

    def _call_function(self, function_call: dict, world: Any = None):
        """
        Call a GPT function.
        """
        function_name = function_call["name"]

        if function_name not in CALLABLE_FUNCTION_TABLE:
            return {'error': f'"{function_name}" is not a callable function.'}

        try:
            print_in_color(
                f'    {self.name} is calling function {function_name} ...', 'blue')
            function_info = CALLABLE_FUNCTION_TABLE[function_name]
            function_to_call = function_info['function']
            has_world_param = function_info['has_world_param']
            has_agent_param = function_info['has_agent_param']
            has_agent_name_param = function_info['has_agent_name_param']

            function_args = function_call["arguments"]
            if function_args is None:
                function_args = dict()
            else:
                function_args: dict = json.loads(function_args)

            print_in_color(f'        with arguments {function_args}', 'blue')
            if has_world_param:
                function_args[WORLD_PARAM_NAME] = world
            if has_agent_param:
                function_args[AGENT_PARAM_NAME] = self
            if has_agent_name_param:
                function_args[AGENT_NAME_PARAM_NAME] = self.name

            function_response = function_to_call(**function_args)
            if function_response is None:
                return None

            print_in_color(f'        response: {function_response}', 'blue')
            return function_response
        except Exception as e:
            print_in_color(f'        error: {e}', 'red')
            return {'error': str(e)}

    def receive_message(self, message: dict, print_output: bool = True):
        """
        Receive a message.

        Args:
            message (dict): The message to receive.
            print_output (bool, optional): Whether to print out the message. Defaults to True.
        """
        if print_output:
            print_in_color(
                f'{self.name} received a message: {message["content"]}', 'green')
        self.memory.append(message)

    def response_to_message(self, message: Union[dict, list], store_in_memory: bool = False,
                            print_output: bool = True):
        """
        Respond to a message or multiple messages.

        Args:
            message (Union[dict, list]): The message or messages to respond to.
            store_in_memory (bool, optional): Whether to store the message in memory. Defaults to False.
            print_output (bool, optional): Whether to print out the message and the response. Defaults to True.

        Returns:
            dict: The response.
        """
        new_memory = self.memory.copy()
        if isinstance(message, dict):
            message = [message]
        for m in message:
            if print_output:
                print_in_color(
                    f'{self.name} is asked: {m["content"]}', 'green')
            new_memory.append(m)
        response = stream_chat_completion(
            engine=self.engine,
            messages=new_memory,
            print_output=not self.ignore_none_function_messages,
            **self.engine_args
        )
        if print_output:
            print_in_color(f'{self.name} >> {response["content"]}', 'yellow')
        if store_in_memory:
            self.memory += message
            self.memory.append(response)
        return response

    def think_and_act(self, world: Any = None):
        """
        Think and act.

        Args:
            world (Any, optional): The world. Defaults to None.
        """
        for _ in range(self.function_call_repeats):
            print_in_color(f'{self.name} >> ', 'yellow')
            callable_functions = self._callable_function_descriptions()
            if callable_functions:
                new_message = stream_chat_completion(
                    engine=self.engine,
                    messages=self.memory,
                    print_output=not self.ignore_none_function_messages,
                    functions=callable_functions,
                    function_call="auto",
                    **self.engine_args
                )
            else:
                new_message = stream_chat_completion(
                    engine=self.engine,
                    messages=self.memory,
                    print_output=not self.ignore_none_function_messages,
                    **self.engine_args
                )

            if new_message.get("function_call"):
                self.memory.append(new_message)
                function_response = self._call_function(
                    new_message["function_call"], world=world)
                if function_response is None:
                    function_response = 'done'
                self.memory.append(
                    {
                        "role": "function",
                        "name": new_message["function_call"]["name"],
                        "content": json.dumps(function_response, default=str),
                    }
                )
            else:
                if not self.ignore_none_function_messages:
                    self.memory.append(new_message)
                break


class World:
    agents: Dict[str, Agent] = dict()

    def add_agent(self, name: str, prompt: str,
                  role: str = 'agent',
                  engine: str = 'gpt-3.5-turbo-16k',
                  function_call_repeats: int = DEFAULT_FUNCTION_CALL_REPEATS,
                  ignore_none_function_messages: bool = DEFAULT_IGNORE_NONE_FUNCTION_MESSAGES):
        """
        Add an agent to the world.

        Args:
            name (str): The name of the agent.
            prompt (str): The prompt to start the agent with.
            role (str, optional): The role of the agent. Defaults to 'agent'.
            engine (str, optional): The GPT engine to use. Defaults to 'gpt-3.5-turbo-16k'.
            function_call_repeats (int, optional): The number of times to repeat function calls in agent.think_and_act().
            ignore_none_function_messages (bool, optional): Whether to ignore messages that does not involve function calling.
        """
        agent = Agent(name, prompt, role=role, engine=engine,
                      function_call_repeats=function_call_repeats,
                      ignore_none_function_messages=ignore_none_function_messages)
        self.agents[name] = agent

    def add(self, agent: Agent):
        """
        Add an agent to the world.

        Args:
            agent (Agent): The agent to add.
        """
        self.agents[agent.name] = agent
