import bpy
from .op_import_instruction import IMPORT_INSTRUCTION_OT_Operator
from .op_apply_instruction import APPLY_INSTRUCTION_OT_Operator
from .op_render_sequence import RENDER_SEQUENCE_OT_Operator

classes = [
    IMPORT_INSTRUCTION_OT_Operator,
    APPLY_INSTRUCTION_OT_Operator,
    RENDER_SEQUENCE_OT_Operator
]

def register():
    for cls in classes:
        if not hasattr(bpy.types, cls.__name__):
            bpy.utils.register_class(cls)

def unregister():
    for cls in reversed(classes):
        if hasattr(bpy.types, cls.__name__):
            bpy.utils.unregister_class(cls)

# Make these functions explicitly available at module level
__all__ = ["register", "unregister"]
