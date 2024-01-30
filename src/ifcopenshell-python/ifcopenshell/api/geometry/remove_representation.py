# IfcOpenShell - IFC toolkit and geometry engine
# Copyright (C) 2021 Dion Moult <dion@thinkmoult.com>
#
# This file is part of IfcOpenShell.
#
# IfcOpenShell is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# IfcOpenShell is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with IfcOpenShell.  If not, see <http://www.gnu.org/licenses/>.

import ifcopenshell.util.element


class Usecase:
    def __init__(self, file, **settings):
        """Remove a representation.

        Also purges representation items and their related elements
        like IfcStyledItem, tessellated facesets colours and UV map.

        :param representation: IfcRepresentation to remove.
            Note that it's expected that IfcRepresentation won't be in use
            before calling this method (in such elements as IfcProductRepresentation, IfcShapeAspect)
            otherwise representation won't be removed.
        :type representation: ifcopenshell.entity_instance
        :return: None
        :rtype: None
        """
        self.file = file
        self.settings = {"representation": None}
        for key, value in settings.items():
            self.settings[key] = value

    def execute(self):
        styled_items = set()
        presentation_layer_assignments = set()
        textures = set()
        colours = set()
        for subelement in self.file.traverse(self.settings["representation"]):
            if subelement.is_a("IfcRepresentationItem"):
                [styled_items.add(s) for s in subelement.StyledByItem or []]
                [presentation_layer_assignments.add(s) for s in subelement.LayerAssignment]
                # IfcTessellatedFaceSet inverses
                [textures.add(t) for t in getattr(subelement, "HasTextures", []) or []]
                [colours.add(t) for t in getattr(subelement, "HasColours", []) or []]
            elif subelement.is_a("IfcRepresentation"):
                for layer in subelement.LayerAssignments:
                    presentation_layer_assignments.add(layer)

        ifcopenshell.util.element.remove_deep2(
            self.file,
            self.settings["representation"],
            also_consider=list(styled_items | presentation_layer_assignments | colours),
            do_not_delete=self.file.by_type("IfcGeometricRepresentationContext"),
        )

        for texture in textures:
            ifcopenshell.util.element.remove_deep2(self.file, texture)
        for colour in colours:
            ifcopenshell.util.element.remove_deep2(self.file, colour)

        to_delete = getattr(self.file, "to_delete", ())
        for element in styled_items:
            if not element.Item or element.Item in to_delete:
                self.file.remove(element)
        for element in presentation_layer_assignments:
            if all(item in to_delete for item in element.AssignedItems):
                self.file.remove(element)
