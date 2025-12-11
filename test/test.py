import sys
from pathlib import Path

# Add package root to sys.path so core can be imported
sys.path.append(str(Path(__file__).resolve().parent.parent))

from core.instruction_parser import parse_instruction

# Test JSON
instruction_json = '{"sequence": {"fps": 30, "tracks": []}}'

instruction = parse_instruction(instruction_json)

print("Normalized instruction dict:")
print(instruction)
