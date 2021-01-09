# Copyright (c) 2021 The Toltec Contributors
# SPDX-License-Identifier: MIT
"""
Parse recipes.

A package is a final user-installable software archive. A recipe is a Bash file
which contains the instructions necessary to build one or more related
packages (in the latter case, it is called a split package).
"""

from itertools import product
from typing import Optional
import os
import textwrap
import dateutil.parser
from . import bash, version


class RecipeError(Exception):
    """Raised when a recipe contains an error."""


class Recipe:  # pylint:disable=too-many-instance-attributes,disable=too-few-public-methods
    """Load and execute recipes."""

    def __init__(self, name: str, definition: str):
        """
        Load a recipe from a Bash source.

        :param name: name of the recipe
        :param definition: source string of the recipe
        :raises RecipeError: if the recipe contains an error
        """
        self.name = name
        variables, functions = bash.get_declarations(definition)
        self.variables = variables
        self.functions = functions

        # Parse and check recipe metadata
        pkgnames = _check_field_indexed(variables, "pkgnames")
        timestamp_str = _check_field_string(variables, "timestamp")

        try:
            self.timestamp = dateutil.parser.isoparse(timestamp_str)
        except ValueError as err:
            raise RecipeError(
                "Field 'timestamp' does not contain a \
valid ISO-8601 date"
            ) from err

        self.maintainer = _check_field_string(variables, "maintainer")
        self.image = _check_field_string(variables, "image", "")
        sources = _check_field_indexed(variables, "source", [])
        noextract = _check_field_indexed(variables, "noextract", [])
        sha256sums = _check_field_indexed(variables, "sha256sums", [])

        if len(sources) != len(sha256sums):
            raise RecipeError(
                f"Expected the same number of sources \
and checksums, got {len(sources)} source(s) and \
{len(sha256sums)} checksum(s)"
            )

        self.sources = dict(zip(sources, sha256sums))
        self.noextract = set(noextract)

        # Parse recipe build hooks
        self.actions = {}

        if self.image and "build" not in functions:
            raise RecipeError(
                "Missing build() function for a recipe \
which declares a build image"
            )

        if not self.image and "build" in functions:
            raise RecipeError(
                "Missing image declaration for a recipe \
which has a build() step"
            )

        self.actions["prepare"] = functions.get("prepare", "")
        self.actions["build"] = functions.get("build", "")

        # Parse packages contained in the recipe
        self.packages = {}

        if len(pkgnames) == 1:
            pkg_name = pkgnames[0]
            self.packages[pkg_name] = Package(name, self, definition)
        else:
            for pkg_name in pkgnames:
                if pkg_name not in functions:
                    raise RecipeError(
                        "Missing required function \
{pkg_name}() for corresponding package"
                    )

                self.packages[pkg_name] = Package(
                    pkg_name, self, functions[pkg_name]
                )

    @classmethod
    def from_file(cls, path: str) -> "Recipe":
        """Load a recipe from a file."""
        name = os.path.basename(path)
        with open(os.path.join(path, "package"), "r") as recipe:
            return Recipe(name, recipe.read())


class Package:  # pylint:disable=too-many-instance-attributes
    """Load and execute a package from a recipe."""

    def __init__(self, name: str, parent: Recipe, definition: str):
        """
        Load a package from a Bash source.

        :param name: name of the package
        :param parent: recipe which declares this package
        :param definition: source string of the package (either the full recipe
            script if it contains only a single package, or the package
            script for split packages)
        :raises RecipeError: if the package contains an error
        """
        self.name = name
        self.parent = parent
        parent_variables = bash.put_variables(
            {
                **parent.variables,
                "pkgname": name,
            }
        )
        variables, functions = bash.get_declarations(
            parent_variables + definition
        )
        self.variables = variables
        self.functions = functions = {**parent.functions, **functions}

        # Parse and check package metadata
        pkgver_str = _check_field_string(variables, "pkgver")
        self.version = version.Version.parse(pkgver_str)

        self.arch = _check_field_string(variables, "arch", "armv7-3.2")
        self.desc = _check_field_string(variables, "pkgdesc")
        self.url = _check_field_string(variables, "url")
        self.section = _check_field_string(variables, "section")
        self.license = _check_field_string(variables, "license")
        self.depends = _check_field_indexed(variables, "depends", [])
        self.conflicts = _check_field_indexed(variables, "conflicts", [])

        if "package" not in functions:
            raise RecipeError(
                "Missing required function package() \
for package {self.name}"
            )

        self.action = functions["package"]
        self.install = {}

        for action in ("preinstall", "configure"):
            self.install[action] = functions.get(action, "")

        for rel, step in product(("pre", "post"), ("remove", "upgrade")):
            self.install[rel + step] = functions.get(rel + step, "")

    def pkgid(self) -> str:
        """Get the unique identifier of this package."""
        return "_".join((self.name, str(self.version), self.arch))

    def filename(self) -> str:
        """Get the name of the archive corresponding to this package."""
        return self.pkgid() + ".ipk"

    def control_fields(self) -> str:
        """Get the control fields for this package."""
        control = textwrap.dedent(
            f"""\
            Package: {self.name}
            Version: {self.version}
            Maintainer: {self.parent.maintainer}
            Section: {self.section}
            Architecture: {self.arch}
            Description: {self.desc}
            HomePage: {self.url}
            License: {self.license}
            """
        )

        if self.depends:
            control += (
                "Depends: "
                + ", ".join(item for item in self.depends if item)
                + "\n"
            )

        if self.conflicts:
            control += (
                "Conflicts: "
                + ", ".join(item for item in self.conflicts if item)
                + "\n"
            )

        return control


# Helpers to check that fields of the right type are defined in a recipe
# and to otherwise return a default value
def _check_field_string(
    variables: bash.Variables, name: str, default: Optional[str] = None
) -> str:
    if name not in variables:
        if default is None:
            raise RecipeError(f"Missing required field {name}")
        return default

    value = variables[name]

    if not isinstance(value, str):
        raise RecipeError(
            f"Field {name} must be a string, \
got {type(variables[name]).__name__}"
        )

    return value


def _check_field_indexed(
    variables: bash.Variables,
    name: str,
    default: Optional[bash.IndexedArray] = None,
) -> bash.IndexedArray:
    if name not in variables:
        if default is None:
            raise RecipeError(f"Missing required field '{name}'")
        return default

    value = variables[name]

    if not isinstance(value, list):
        raise RecipeError(
            f"Field '{name}' must be an indexed array, \
got {type(variables[name]).__name__}"
        )

    return value
