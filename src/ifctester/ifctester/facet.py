# IfcTester - IDS based model auditing
# Copyright (C) 2021 Artur Tomczak <artomczak@gmail.com>, Thomas Krijnen <mail@thomaskrijnen.com>, Dion Moult <dion@thinkmoult.com>
#
# This file is part of IfcTester.
#
# IfcTester is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# IfcTester is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with IfcTester.  If not, see <http://www.gnu.org/licenses/>.

import re
import builtins
import ifcopenshell.util.unit
import ifcopenshell.util.element
import ifcopenshell.util.classification
from xmlschema.validators import identities


def cast_to_value(from_value, to_value):
    try:
        target_type = type(to_value).__name__
        if target_type == "int":
            # Casting str -> float means that notation like '1e3' is preserved
            # We do not cast to int because 42.0 == 42 and 42.3 != 42
            return float(from_value)
        elif target_type == "bool":
            if from_value == "TRUE":
                return True
            elif from_value == "FALSE":
                return False
        return builtins.__dict__[target_type](from_value)
    except ValueError:
        pass


class Facet:
    def __init__(self, *parameters):
        self.status = None
        self.failed_entities = []
        self.failed_reasons = []
        for i, name in enumerate(self.parameters):
            setattr(self, name.replace("@", ""), parameters[i])

    def asdict(self):
        results = {}
        for name in self.parameters:
            value = getattr(self, name.replace("@", ""))
            if value is not None:
                results[name] = value if "@" in name else self.to_ids_value(value)
        return results

    def parse(self, xml):
        for name, value in xml.items():
            name = name.replace("@", "")
            if isinstance(value, dict) and "simpleValue" in value.keys():
                setattr(self, name, value["simpleValue"])
            elif isinstance(value, dict) and "restriction" in value.keys():
                setattr(self, name, Restriction().parse(value["restriction"][0]))
                # TODO handle more than one restriction: return [restriction(r) for r in v["restriction"]]
            else:
                setattr(self, name, value)
        return self

    def filter(self, ifc_file, elements):
        return [e for e in elements if self(e)]

    def to_string(self, clause_type):
        if clause_type == "applicability":
            templates = self.applicability_templates
        elif clause_type == "requirement":
            templates = self.requirement_templates

        for template in templates:
            total_variables = len(template) - len(template.replace("{", ""))
            total_replacements = 0
            for key in self.parameters:
                key = key.replace("@", "")
                value = getattr(self, key)
                key_variable = "{" + key + "}"
                if value is not None and key_variable in template:
                    template = template.replace(key_variable, str(value))
                    total_replacements += 1
                if total_replacements == total_variables:
                    return template

    def to_ids_value(self, parameter):
        if isinstance(parameter, str):
            parameter_dict = {"simpleValue": parameter}
        elif isinstance(parameter, Restriction):
            parameter_dict = {"xs:restriction": [parameter.asdict()]}
        elif isinstance(parameter, list):
            restrictions = {"@base": "xs:" + parameter[0].base}
            for p in parameter:
                x = p.asdict()
                restrictions[list(x)[1]] = x[list(x)[1]]
            parameter_dict = {"xs:restriction": [restrictions]}
        else:
            raise Exception(str(parameter) + " was not able to be converted into 'Parameter_dict'")
        return parameter_dict


class Entity(Facet):
    def __init__(self, name="IFCWALL", predefinedType=None, instructions=None):
        self.parameters = ["name", "predefinedType", "@instructions"]
        self.applicability_templates = [
            "All {name} data of type {predefinedType}",
            "All {name} data",
        ]
        self.requirement_templates = [
            "Shall be {name} data of type {predefinedType}",
            "Shall be {name} data",
        ]
        super().__init__(name, predefinedType, instructions)

    def filter(self, ifc_file, elements):
        if isinstance(self.name, str):
            results = ifc_file.by_type(self.name, include_subtypes=False)
        else:
            results = []
            ifc_classes = [t for t in ifc_file.wrapped_data.types() if t.upper() == self.name]
            [results.extend(ifc_file.by_type(ifc_class, include_subtypes=False)) for ifc_class in ifc_classes]
        if self.predefinedType:
            return [r for r in results if self(r)]
        return results

    def __call__(self, inst, logger=None):
        is_pass = inst.is_a().upper() == self.name
        reason = None

        if not is_pass:
            reason = {"type": "NAME", "actual": inst.is_a().upper()}

        if is_pass and self.predefinedType:
            predefined_type = ifcopenshell.util.element.get_predefined_type(inst)
            is_pass = predefined_type == self.predefinedType

            if not is_pass:
                reason = {"type": "PREDEFINEDTYPE", "actual": predefined_type}

        return EntityResult(is_pass, reason)


class Attribute(Facet):
    def __init__(self, name="Name", value=None, minOccurs=None, maxOccurs=None, instructions=None):
        self.parameters = ["name", "value", "@minOccurs", "@maxOccurs", "@instructions"]
        self.applicability_templates = [
            "Data where the {name} is {value}",
            "Data where the {name} is provided",
        ]
        self.requirement_templates = [
            "The {name} shall be {value}",
            "The {name} shall be provided",
        ]
        super().__init__(name, value, minOccurs, maxOccurs, instructions)

    def __call__(self, inst, logger=None):
        if self.minOccurs == 0 and self.maxOccurs != 0:
            return AttributeResult(True)

        def get_values(element, name):
            if isinstance(name, str):
                return [getattr(element, name, None)]
            return [v for k, v in element.get_info().items() if k == name]

        element_type = ifcopenshell.util.element.get_type(inst)

        if isinstance(self.name, str):
            type_value = getattr(element_type, self.name, None) if element_type else None
            occurrence_value = getattr(inst, self.name, None)
            names = [self.name]
            values = [occurrence_value if occurrence_value is not None else type_value]
        else:
            if element_type:
                info = element_type.get_info()
                info.update({k: v for k, v in inst.get_info().items() if v is not None})
            else:
                info = inst.get_info()
            names = []
            values = []
            for k, v in info.items():
                if k == self.name:
                    names.append(k)
                    values.append(v)

        is_pass = bool(values)
        reason = None

        if not is_pass:
            reason = {"type": "NOVALUE"}

        if is_pass:
            for i, value in enumerate(values):
                if value is None:
                    is_pass = False
                    reason = {"type": "FALSEY", "actual": value}
                elif value == "":
                    is_pass = False
                    reason = {"type": "FALSEY", "actual": value}
                elif value == tuple():
                    is_pass = False
                    reason = {"type": "FALSEY", "actual": value}
                else:
                    argument_index = inst.wrapped_data.get_argument_index(names[i])
                    try:
                        attribute_type = inst.attribute_type(argument_index)
                        if attribute_type == "LOGICAL" and value == "UNKNOWN":
                            is_pass = False
                            reason = {"type": "FALSEY", "actual": value}
                    except:
                        if names[i] in inst.wrapped_data.get_inverse_attribute_names():
                            is_pass = False
                            reason = {"type": "INVALID"}
                if not is_pass:
                    break

        if is_pass and self.value:
            for value in values:
                if isinstance(value, ifcopenshell.entity_instance):
                    is_pass = False
                    reason = {"type": "VALUE", "actual": value}
                    break
                elif isinstance(self.value, str) and isinstance(value, str):
                    if value != self.value:
                        is_pass = False
                        reason = {"type": "VALUE", "actual": value}
                        break
                elif isinstance(self.value, str):
                    cast_value = cast_to_value(self.value, value)
                    if isinstance(value, float) and isinstance(cast_value, float):
                        if value < cast_value * (1.0 - 1e-6) or value > cast_value * (1.0 + 1e-6):
                            is_pass = False
                            reason = {"type": "VALUE", "actual": value}
                            break
                    elif value != cast_value:
                        is_pass = False
                        reason = {"type": "VALUE", "actual": value}
                        break
                elif value != self.value:
                    is_pass = False
                    reason = {"type": "VALUE", "actual": value}
                    break

        if self.maxOccurs == 0:
            return AttributeResult(not is_pass, {"type": "PROHIBITED"})
        return AttributeResult(is_pass, reason)


class Classification(Facet):
    def __init__(self, value=None, system=None, uri=None, minOccurs=None, maxOccurs=None, instructions=None):
        self.parameters = ["value", "system", "@uri", "@minOccurs", "@maxOccurs", "@instructions"]
        self.applicability_templates = [
            "Data having a {system} reference of {value}",
            "Data classified using {system}",
            "Data classified as {value}",
        ]
        self.requirement_templates = [
            "Shall have a {system} reference of {value}",
            "Shall be classified using {system}",
            "Shall be classified as {value}",
        ]
        super().__init__(value, system, uri, minOccurs, maxOccurs, instructions)

    def filter(self, ifc_file, elements):
        pass

    def __call__(self, inst, logger=None):
        if self.minOccurs == 0 and self.maxOccurs != 0:
            return ClassificationResult(True)

        leaf_references = ifcopenshell.util.classification.get_references(inst)

        references = leaf_references.copy()
        for leaf_reference in leaf_references:
            references.update(ifcopenshell.util.classification.get_inherited_references(leaf_reference))

        is_pass = bool(references)
        reason = None

        if not is_pass:
            reason = {"type": "NOVALUE"}

        if is_pass and self.value:
            values = [getattr(r, "Identification", getattr(r, "ItemReference", None)) for r in references]
            is_pass = any([self.value == v for v in values])
            if not is_pass:
                reason = {"type": "VALUE", "actual": values}

        if is_pass and self.system:
            systems = [ifcopenshell.util.classification.get_classification(r).Name for r in references]
            is_pass = any([self.system == s for s in systems])
            if not is_pass:
                reason = {"type": "SYSTEM", "actual": systems}

        if self.maxOccurs == 0:
            return ClassificationResult(not is_pass, {"type": "PROHIBITED"})
        return ClassificationResult(is_pass, reason)


class PartOf(Facet):
    def __init__(self, entity=None, relation="IfcRelAggregates", minOccurs=None, maxOccurs=None, instructions=None):
        self.parameters = ["entity", "@relation", "@minOccurs", "@maxOccurs", "@instructions"]
        self.applicability_templates = [
            "An element with an {relation} relationship with an {entity}",
            "An element with an {relation} relationship",
        ]
        self.requirement_templates = [
            "An element must have an {relation} relationship with an {entity}",
            "An element must have an {relation} relationship",
        ]
        super().__init__(entity, relation, minOccurs, maxOccurs, instructions)

    def __call__(self, inst, logger=None):
        if self.minOccurs == 0 and self.maxOccurs != 0:
            return PartOfResult(True)

        reason = None
        if self.relation == "IfcRelAggregates":
            aggregate = ifcopenshell.util.element.get_aggregate(inst)
            is_pass = aggregate is not None
            if not is_pass:
                reason = {"type": "RELATION"}
            if is_pass and self.entity:
                is_pass = False
                ancestors = []
                while aggregate is not None:
                    ancestors.append(aggregate.is_a())
                    if aggregate.is_a().upper() == self.entity:
                        is_pass = True
                        break
                    aggregate = ifcopenshell.util.element.get_aggregate(aggregate)
                if not is_pass:
                    reason = {"type": "ENTITY", "actual": ancestors}
        elif self.relation == "IfcRelAssignsToGroup":
            group = None
            for rel in getattr(inst, "HasAssignments", []) or []:
                if rel.is_a("IfcRelAssignsToGroup"):
                    group = rel.RelatingGroup
                    break
            is_pass = group is not None
            if not is_pass:
                reason = {"type": "NOVALUE"}
            if is_pass and self.entity:
                if group.is_a().upper() != self.entity:
                    is_pass = False
                    reason = {"type": "ENTITY", "actual": group.is_a().upper()}
        elif self.relation == "IfcRelContainedInSpatialStructure":
            container = ifcopenshell.util.element.get_container(inst)
            is_pass = container is not None
            if not is_pass:
                reason = {"type": "RELATION"}
            if is_pass and self.entity:
                if container.is_a().upper() != self.entity:
                    is_pass = False
                    reason = {"type": "ENTITY", "actual": container.is_a().upper()}
        elif self.relation == "IfcRelNests":
            nest = self.get_nested_whole(inst)
            is_pass = nest is not None
            if not is_pass:
                reason = {"type": "NOVALUE"}
            if is_pass and self.entity:
                is_pass = False
                ancestors = []
                while nest is not None:
                    ancestors.append(nest.is_a())
                    if nest.is_a().upper() == self.entity:
                        is_pass = True
                        break
                    nest = self.get_nested_whole(nest)
                if not is_pass:
                    reason = {"type": "ENTITY", "actual": ancestors}

        if self.maxOccurs == 0:
            return PartOfResult(not is_pass, {"type": "PROHIBITED"})
        return PartOfResult(is_pass, reason)

    def get_nested_whole(self, element):
        for rel in getattr(element, "Nests", []) or []:
            return rel.RelatingObject


class Property(Facet):
    def __init__(
        self,
        propertySet="Property_Set",
        name="PropertyName",
        value=None,
        measure=None,
        uri=None,
        minOccurs=None,
        maxOccurs=None,
        instructions=None,
    ):
        self.parameters = [
            "propertySet",
            "name",
            "value",
            "@measure",
            "@uri",
            "@minOccurs",
            "@maxOccurs",
            "@instructions",
        ]
        self.applicability_templates = [
            "Elements with {name} data of {value} in the dataset {propertySet}",
            "Elements with {name} data in the dataset {propertySet}",
        ]
        self.requirement_templates = [
            "{name} data shall be {value} and in the dataset {propertySet}",
            "{name} data shall be provided in the dataset {propertySet}",
        ]
        super().__init__(propertySet, name, value, measure, uri, minOccurs, maxOccurs, instructions)

    def __call__(self, inst, logger=None):
        if self.minOccurs == 0 and self.maxOccurs != 0:
            return PropertyResult(True)

        all_psets = ifcopenshell.util.element.get_psets(inst)

        if isinstance(self.propertySet, str):
            pset = all_psets.get(self.propertySet, None)
            psets = {self.propertySet: pset} if pset else {}
        else:
            psets = {k: v for k, v in all_psets.items() if k == self.propertySet}

        is_pass = bool(psets)
        reason = None

        if not is_pass:
            reason = {"type": "NOPSET"}

        if is_pass:
            props = {}
            for pset_name, pset_props in psets.items():
                props[pset_name] = {}
                if isinstance(self.name, str):
                    prop = pset_props.get(self.name)
                    if prop == "UNKNOWN" and [
                        p for p in inst.wrapped_data.file.by_id(pset_props["id"]).HasProperties if p.Name == self.name
                    ][0].NominalValue.is_a("IfcLogical"):
                        pass
                    elif prop is not None and prop != "":
                        props[pset_name][self.name] = prop
                else:
                    props[pset_name] = {k: v for k, v in pset_props.items() if k == self.name}

                if not bool(props[pset_name]):
                    is_pass = False
                    reason = {"type": "NOVALUE"}
                    break

                pset_entity = inst.wrapped_data.file.by_id(pset_props["id"])
                for prop_entity in pset_entity.HasProperties:
                    if (
                        prop_entity.Name not in props[pset_name].keys()
                        or not prop_entity.is_a("IfcPropertySingleValue")
                        or prop_entity.NominalValue is None
                    ):
                        continue

                    data_type = prop_entity.NominalValue.is_a()

                    if data_type != self.measure:
                        is_pass = False
                        reason = {"type": "MEASURE", "actual": data_type}
                        break

                    unit = ifcopenshell.util.unit.get_property_unit(prop_entity, inst.wrapped_data.file)
                    if unit:
                        props[pset_name][prop_entity.Name] = ifcopenshell.util.unit.convert(
                            prop_entity.NominalValue.wrappedValue,
                            getattr(unit, "Prefix", None),
                            unit.Name,
                            None,
                            ifcopenshell.util.unit.si_type_names[unit.UnitType],
                        )

                if not is_pass:
                    break

                if self.value:
                    for value in props[pset_name].values():
                        if isinstance(self.value, str) and isinstance(value, str):
                            if value != self.value:
                                is_pass = False
                                reason = {"type": "VALUE", "actual": value}
                                break
                        elif isinstance(self.value, str):
                            cast_value = cast_to_value(self.value, value)
                            if isinstance(value, float) and isinstance(cast_value, float):
                                if value < cast_value * (1.0 - 1e-6) or value > cast_value * (1.0 + 1e-6):
                                    is_pass = False
                                    reason = {"type": "VALUE", "actual": value}
                                    break
                            elif value != cast_value:
                                is_pass = False
                                reason = {"type": "VALUE", "actual": value}
                                break
                        elif value != self.value:
                            is_pass = False
                            reason = {"type": "VALUE", "actual": value}
                            break

        if self.maxOccurs == 0:
            return PropertyResult(not is_pass, {"type": "PROHIBITED"})
        return PropertyResult(is_pass, reason)


class Material(Facet):
    def __init__(self, value=None, uri=None, minOccurs=None, maxOccurs=None, instructions=None):
        self.parameters = ["value", "@uri", "@minOccurs", "@maxOccurs", "@instructions"]
        self.applicability_templates = [
            "All data with a {value} material",
            "All data with a material",
        ]
        self.requirement_templates = [
            "Shall shall have a material of {value}",
            "Shall have a material",
        ]
        super().__init__(value, uri, minOccurs, maxOccurs, instructions)

    def __call__(self, inst, logger=None):
        if self.minOccurs == 0 and self.maxOccurs != 0:
            return MaterialResult(True)

        material = ifcopenshell.util.element.get_material(inst, should_skip_usage=True)

        is_pass = material is not None
        reason = None

        if not is_pass:
            reason = {"type": "NOVALUE"}

        if is_pass and self.value:
            if material.is_a("IfcMaterial"):
                values = {material.Name, getattr(material, "Category")}
            elif material.is_a("IfcMaterialList"):
                values = set()
                for mat in material.Materials or []:
                    values.update([mat.Name, getattr(mat, "Category")])
            elif material.is_a("IfcMaterialLayerSet"):
                values = {material.LayerSetName}
                for item in material.MaterialLayers or []:
                    values.update([item.Name, item.Category, item.Material.Name, getattr(item.Material, "Category")])
            elif material.is_a("IfcMaterialProfileSet"):
                values = {material.Name}
                for item in material.MaterialProfiles or []:
                    values.update([item.Name, item.Category, item.Material.Name, getattr(item.Material, "Category")])
            elif material.is_a("IfcMaterialConstituentSet"):
                values = {material.Name}
                for item in material.MaterialConstituents or []:
                    values.update([item.Name, item.Category, item.Material.Name, getattr(item.Material, "Category")])

            is_pass = False
            for value in values:
                if value == self.value:
                    is_pass = True
                    break

            if not is_pass:
                reason = {"type": "VALUE", "actual": values}

        if self.maxOccurs == 0:
            return MaterialResult(not is_pass, {"type": "PROHIBITED"})
        return MaterialResult(is_pass, reason)


class Restriction:
    def __init__(self, options="", type="pattern", base="string"):
        if type in ["enumeration", "pattern", "bounds"]:
            self.type = type
            self.base = base
            self.options = options
            if (
                (type == "enumeration" and isinstance(options, list))
                or (type == "bounds" and isinstance(options, dict))
                or (type == "pattern" and isinstance(options, str))
            ):
                self.options = options
            else:
                raise Exception("Options were not properly defined.")

    def parse(self, ids_dict):
        if ids_dict:
            try:
                self.base = ids_dict["@base"][3:]
            except KeyError:
                self.base = "String"

            for n in ids_dict:
                if n == "enumeration":
                    self.type = "enumeration"
                    self.options = []
                    for x in ids_dict[n]:
                        self.options.append(x["@value"])
                elif n[-7:] == "clusive":
                    self.type = "bounds"
                    self.options = {}
                    self.options.append({n: ids_dict[n]["@value"]})
                elif n[-5:] == "ength":
                    self.type = "length"
                    if n[3:6] == "min":
                        self.options.append(">=")
                    elif n[3:6] == "max":
                        self.options.append("<=")
                    else:
                        self.options.append("==")
                    self.options[-1] += str(ids_dict[n]["@value"])
                elif n == "pattern":
                    self.type = "pattern"
                    self.options = ids_dict[n]["@value"]
                # TODO add fractionDigits
                # TODO add totalDigits
                # TODO add whiteSpace
                elif n == "@base":
                    pass
                else:
                    print("Error! Restriction not implemented")
        return self

    def asdict(self):
        rest_dict = {"@base": "xs:" + self.base}
        if self.type == "enumeration":
            for option in self.options:
                if "xs:enumeration" not in rest_dict:
                    rest_dict["xs:enumeration"] = [{"@value": option}]
                else:
                    rest_dict["xs:enumeration"].append({"@value": option})
        elif self.type == "bounds":
            for option in self.options:
                rest_dict["xs:" + option] = [{"@value": str(self.options[option]), "@fixed": False}]
        elif self.type == "pattern":
            if "xs:pattern" not in rest_dict:
                rest_dict["xs:pattern"] = [{"@value": self.options}]
            else:
                rest_dict["xs:pattern"].append({"@value": self.options})
        return rest_dict

    def __eq__(self, other):
        result = False
        if self and (other or other == 0):
            if self.type == "enumeration" and self.base == "bool":
                self.options = [x.lower() for x in self.options]
                result = str(other).lower() in self.options
            elif self.type == "enumeration":
                result = other in [cast_to_value(o, other) for o in self.options]
            elif self.type == "bounds":
                result = True
                for sign in self.options.keys():
                    if sign == "minInclusive" and other < self.options[sign]:
                        result = False
                    elif sign == "maxInclusive" and other > self.options[sign]:
                        result = False
                    elif sign == "minExclusive" and other <= self.options[sign]:
                        result = False
                    elif sign == "maxExclusive" and other >= self.options[sign]:
                        result = False
            elif self.type == "length":
                for op in self.options:
                    if eval(str(len(other)) + op):  # TODO eval not safe?
                        result = True
            elif self.type == "pattern":
                if isinstance(self.options, list):
                    # TODO handle case with multiple pattern options
                    translated_pattern = identities.translate_pattern(self.options[0])
                else:
                    translated_pattern = identities.translate_pattern(self.options)
                regex_pattern = re.compile(translated_pattern)
                if regex_pattern.fullmatch(other) is not None:
                    result = True
            # TODO add fractionDigits
            # TODO add totalDigits
            # TODO add whiteSpace
        return result

    def __str__(self):
        if self.type == "enumeration":
            return "one of '%s'" % "' or '".join(self.options)
        elif self.type == "bounds":
            bounds = {
                "minInclusive": "larger or equal ",
                "maxInclusive": "smaller or equal ",
                "minExclusive": "larger than ",
                "maxExclusive": "smaller than ",
            }
            return "of value %s" % ", and ".join([bounds[x] + str(self.options[x]) for x in self.options])
        elif self.type == "length":
            return "%s letters long" % " and ".join(self.options)
        elif self.type == "pattern":
            return "the pattern '%s'" % self.options
        # TODO add fractionDigits
        # TODO add totalDigits
        # TODO add whiteSpace


class Result:
    def __init__(self, is_pass, reason=None):
        self.is_pass = is_pass
        self.reason = reason

    def __bool__(self):
        return self.is_pass

    def __str__(self):
        return "" if self.is_pass else self.to_string()

    def to_string(self):
        return str(self.reason) or "The requirements were not met for some inexplicable reason. Good luck!"


class EntityResult(Result):
    def to_string(self):
        if self.reason["type"] == "NAME":
            return f"The entity class \"{self.reason['actual']}\" does not meet the required IFC class"
        elif self.reason["type"] == "PREDEFINEDTYPE":
            return f"The predefined type \"{str(self.reason['actual'])}\" does not meet the required type"


class AttributeResult(Result):
    def to_string(self):
        if self.reason["type"] == "NOVALUE":
            return "The required attribute did not exist"
        elif self.reason["type"] == "FALSEY":
            return f"The attribute value \"{str(self.reason['actual'])}\" is empty"
        elif self.reason["type"] == "INVALID":
            return f"An invalid attribute name was specified in the IDS"
        elif self.reason["type"] == "VALUE":
            return f"The attribute value \"{str(self.reason['actual'])}\" does not match the requirement"


class ClassificationResult(Result):
    def to_string(self):
        if self.reason["type"] == "NOVALUE":
            return "The entity has no classification"
        elif self.reason["type"] == "VALUE":
            return f"The references \"{str(self.reason['actual'])}\" do not match the requirements"
        elif self.reason["type"] == "system":
            return f"The systems \"{str(self.reason['actual'])}\" do not match the requirements"


class PartOfResult(Result):
    def to_string(self):
        return "TODO"


class PropertyResult(Result):
    def to_string(self):
        if self.reason["type"] == "NOPSET":
            return "The entity has no property sets"
        elif self.reason["type"] == "NOVALUE":
            return "The property set does not contain the required property"
        elif self.reason["type"] == "MEASURE":
            return f"The data type \"{str(self.reason['actual'])}\" does not match the requirements"
        elif self.reason["type"] == "VALUE" and len(self.reason["actual"]) == 1:
            return f"The property value \"{str(self.reason['actual'][0])}\" does not match the requirements"
        elif self.reason["type"] == "VALUE":
            return f"The property values \"{str(self.reason['actual'])}\" do not match the requirements"


class MaterialResult(Result):
    def to_string(self):
        if self.reason["type"] == "NOVALUE":
            return "The entity has no material"
        elif self.reason["type"] == "VALUE":
            return (
                f"The material names and categories of \"{str(self.reason['actual'])}\" does not match the requirement"
            )
