# ops/__init__.py
import bpy
from .op_import_instruction import IMPORT_INSTRUCTION_OT_Operator
from .op_apply_instruction import APPLY_INSTRUCTION_OT_Operator

classes = [
    IMPORT_INSTRUCTION_OT_Operator,
    APPLY_INSTRUCTION_OT_Operator,
]

def register():
    for cls in classes:
        bpy.utils.register_class(cls)

def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

# Make these functions explicitly available at module level
__all__ = ["register", "unregister"]
