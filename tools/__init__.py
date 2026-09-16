from tools.bash import bash
from tools.edit import edit_file
from tools.glob import glob
from tools.read import read_file
from tools.skill import skill
from tools.subagent import task
from tools.todo import todo_write
from tools.write import write_file

TOOLS = [bash, read_file, write_file, edit_file, glob, todo_write, skill, task]

TOOL_HANDLERS = {t.name: t.handler for t in TOOLS}
