import bpy
from . import ui, operator

classes = (
    operator.AddStyle,
    operator.EditStyle,
    operator.RemoveStyle,
    operator.UnlinkStyle,
    ui.BIM_PT_style,
)


def register():
    pass


def unregister():
    pass
