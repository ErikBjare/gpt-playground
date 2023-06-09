from typing import Generator
import textwrap
import os
import subprocess
import functools
import io
import ast
import logging
from contextlib import redirect_stdout

from termcolor import colored
import openai

from ..util import len_tokens
from ..message import Message
from ..cache import memory

logger = logging.getLogger(__name__)

EMOJI_WARN = "⚠️"


def _print_preview():
    # print a preview section header
    print(colored("Preview", "white", attrs=["bold"]))


def _execute_save(text: str, ask=True) -> Generator[Message, None, None]:
    """Saves a codeblock to a file."""
    # last scanned codeblock
    prev_codeblock = ""
    # currently scanning codeblock
    codeblock = ""

    for line in text.splitlines():
        if line.strip().startswith("// save:"):
            filename = line.split(":")[1].strip()
            content = "\n".join(prev_codeblock.split("\n")[1:-2])
            _print_preview()
            print(f"# filename: {filename}")
            print(textwrap.indent(content, "> "))
            confirm = input("Save to " + filename + "? (Y/n) ")
            if confirm.lower() in ["y", "Y", "", "yes"]:
                with open(filename, "w") as file:
                    file.write(content)
            yield Message("system", "Saved to " + filename)

        if line.startswith("```") or codeblock:
            codeblock += line + "\n"
            # if block if complete
            if codeblock.startswith("```") and codeblock.strip().endswith("```"):
                prev_codeblock = codeblock
                codeblock = ""


def _execute_load(filename: str) -> Generator[Message, None, None]:
    if not os.path.exists(filename):
        yield Message(
            "system", "Tried to load file '" + filename + "', but it does not exist."
        )
    confirm = input("Load from " + filename + "? (Y/n) ")
    if confirm.lower() in ["y", "Y", "", "yes"]:
        with open(filename, "r") as file:
            data = file.read()
        yield Message("system", f"# filename: {filename}\n\n{data}")


def _execute_linecmd(line: str) -> Generator[Message, None, None]:
    """Executes a line command and returns the response."""
    if line.startswith("terminal: "):
        cmd = line[len("terminal: ") :]
        yield from _execute_shell(cmd)
    elif line.startswith("python: "):
        cmd = line[len("python: ") :]
        yield from _execute_python(cmd)
    elif line.strip().startswith("// load: "):
        filename = line[len("load: ") :]
        yield from _execute_load(filename)


def _execute_codeblock(codeblock: str) -> Generator[Message, None, None]:
    """Executes a codeblock and returns the output."""
    codeblock_lang = codeblock.splitlines()[0].strip()
    codeblock = codeblock[len(codeblock_lang) :]
    if codeblock_lang in ["python"]:
        yield from _execute_python(codeblock)
    elif codeblock_lang in ["terminal", "bash", "sh"]:
        yield from _execute_shell(codeblock)
    else:
        logger.warning(f"Unknown codeblock type {codeblock_lang}")


def _execute_shell(cmd: str, ask=True) -> Generator[Message, None, None]:
    """Executes a shell command and returns the output."""
    cmd = cmd.strip()
    if cmd.startswith("$ "):
        cmd = cmd[len("$ ") :]
    if ask:
        _print_preview()
        print("$ " + colored(cmd, "light_yellow"))
        confirm = input(
            colored(
                f"{EMOJI_WARN} Execute command in terminal? (Y/n) ",
                "red",
                "on_light_yellow",
            )
        )
    if not ask or confirm.lower() in ["y", "Y", "", "yes"]:
        p = subprocess.run(cmd, capture_output=True, shell=True, text=True)
        stdout = p.stdout.strip()
        stderr = p.stderr.strip()
        msg = f"Ran command:\n```bash\n{cmd}\n```\n\n"
        if stdout:
            msg += f"Output:\n\n```bash\n{stdout}\n```\n\n"
        if stderr:
            msg += f"Error:\n\n```bash\n{stderr}\n```\n\n"
        if not stdout and not stderr:
            msg += "No output\n\n"
        if p.returncode != 0:
            msg += f"Return code: {p.returncode}"
        else:
            msg += "Ran successfully"

        yield Message("system", msg)


locals_ = locals()


def _capture_output(func):
    """Captures stdout and stderr from a function and returns them as a string."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with io.StringIO() as buf, redirect_stdout(buf):
            func(*args, **kwargs)
            yield buf.getvalue()

    return wrapper


def _execute_python(code: str, ask=True) -> Generator[Message, None, None]:
    """Executes a python codeblock and returns the output."""
    code = code.strip()
    if ask:
        _print_preview()
        print(">>> " + colored(code, "light_yellow"))
        confirm = input(
            colored(
                f" {EMOJI_WARN} Execute Python code? (y/N) ",
                "red",
                "on_light_yellow",
                attrs=["bold"],
            )
        )

    error_during_execution = False
    if not ask or confirm.lower() in ["y", "yes"]:
        # parse code into statements
        try:
            statements = ast.parse(code).body
        except SyntaxError as e:
            yield Message("system", f"SyntaxError: {e}")
            return

        output = ""
        # execute statements
        for stmt in statements:
            stmt_str = ast.unparse(stmt)
            output += ">>> " + stmt_str + "\n"
            try:
                # if stmt is assignment or function def, have to use exec
                if (
                    isinstance(stmt, ast.Assign)
                    or isinstance(stmt, ast.AnnAssign)
                    or isinstance(stmt, ast.Assert)
                    or isinstance(stmt, ast.ClassDef)
                    or isinstance(stmt, ast.FunctionDef)
                    or isinstance(stmt, ast.Import)
                    or isinstance(stmt, ast.ImportFrom)
                    or isinstance(stmt, ast.If)
                    or isinstance(stmt, ast.For)
                    or isinstance(stmt, ast.While)
                    or isinstance(stmt, ast.With)
                    or isinstance(stmt, ast.Try)
                    or isinstance(stmt, ast.AsyncFor)
                    or isinstance(stmt, ast.AsyncFunctionDef)
                    or isinstance(stmt, ast.AsyncWith)
                ):
                    with io.StringIO() as buf, redirect_stdout(buf):
                        exec(stmt_str, globals(), locals_)
                        result = buf.getvalue().strip()
                else:
                    result = eval(stmt_str, globals(), locals_)
                if result:
                    output += str(result) + "\n"
            except Exception as e:
                output += f"{e.__class__.__name__}: {e}\n"
                error_during_execution = True
                break
        if error_during_execution:
            output += "Error during execution, aborting."
        yield Message("system", output)
    else:
        yield Message("system", "Aborted, user chose not to run command.")


def test_execute_python():
    assert _execute_python("1 + 1", ask=False) == ">>> 1 + 1\n2\n"
    assert _execute_python("a = 2\na", ask=False) == ">>> a = 2\n>>> a\n2\n"
    assert _execute_python("print(1)", ask=False) == ">>> print(1)\n"


@memory.cache
def _llm_summarize(content: str) -> str:
    """Summarizes a long text using a LLM algorithm."""
    response = openai.Completion.create(
        model="text-davinci-003",
        prompt="Please summarize the following:\n" + content + "\n\nSummary:",
        temperature=0,
        max_tokens=256,
    )
    summary = response.choices[0].text
    logger.info(
        f"Summarized long output ({len_tokens(content)} -> {len_tokens(summary)} tokens): "
        + summary
    )
    return summary


def summarize(msg: Message) -> Message:
    """Uses a cheap LLM to summarize long outputs."""
    if len_tokens(msg.content) > 200:
        # first 100 tokens
        beginning = " ".join(msg.content.split()[:150])
        # last 100 tokens
        end = " ".join(msg.content.split()[-100:])
        summary = _llm_summarize(beginning + "\n...\n" + end)
    else:
        summary = _llm_summarize(msg.content)
    return Message("system", f"Here is a summary of the response:\n{summary}")
