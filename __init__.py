bl_info = {
    "name": "VSE Instructor",
    "author": "Immanuel Savvy",
    "version": (0, 1),
    "blender": (4, 5, 0),
    "location": "VSE > Sidebar > VSE Instructor",
    "description": "Build Blender VSE sequences from JSON instructions",
    "category": "Sequencer",
}

import bpy

# -----------------------------
# Import Submodules
# -----------------------------
from . import ops
from . import ui

# -----------------------------
# Registration
# -----------------------------
def register():
    ops.register()
    ui.register()
    print("VSE Instructor registered")

def unregister():
    ui.unregister()
    ops.unregister()
    print("VSE Instructor unregistered")

if __name__ == "__main__":
    register()
