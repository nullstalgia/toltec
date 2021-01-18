# Copyright (c) 2021 The Toltec Contributors
# SPDX-License-Identifier: MIT
"""Build recipes and create packages."""

from collections.abc import Iterable
import shutil
from typing import Optional
import re
import os
import glob
import logging
import textwrap
import docker
import requests
from . import bash, util, ipk, paths
from .recipe import Recipe, Package

logger = logging.getLogger(__name__)


class BuildError(Exception):
    """Raised when a build step fails."""


class BuildContextAdapter(logging.LoggerAdapter):
    """Prefix log entries with information about the current build target."""

    def process(self, msg, kwargs):
        if "recipe" in self.extra:
            if "package" in self.extra:
                return (
                    "%s (%s): %s"
                    % (self.extra["package"], self.extra["recipe"], msg),
                    kwargs,
                )

            return "%s: %s" % (self.extra["recipe"], msg), kwargs

        return msg, kwargs


class Builder:  # pylint: disable=too-few-public-methods
    """Helper class for building recipes."""

    # Detect non-local paths
    URL_REGEX = re.compile(r"[a-z]+://")

    # Prefix for all Toltec Docker images
    IMAGE_PREFIX = "ghcr.io/toltec-dev/"

    # Toltec Docker image used for generic tasks
    DEFAULT_IMAGE = "base:v1.2.2"

    def __init__(self):
        """Create a builder helper."""
        os.makedirs(paths.WORK_DIR, exist_ok=True)
        os.makedirs(paths.REPO_DIR, exist_ok=True)

        self.install_lib = ""
        install_lib_path = os.path.join(paths.SCRIPTS_DIR, "install-lib")

        with open(install_lib_path, "r") as file:
            for line in file:
                if not line.strip().startswith("#"):
                    self.install_lib += line

        try:
            self.docker = docker.from_env()
        except docker.errors.DockerException as err:
            raise BuildError(
                "Unable to connect to the Docker daemon. \
Please check that the service is running and that you have the necessary \
permissions."
            ) from err

    def make(
        self, recipe_name: str, packages_names: Optional[Iterable[str]] = None
    ) -> bool:
        """
        Build a recipe and create its associated packages.

        :param recipe_name: name of the recipe to make
        :param packages_names: list of packages names of the recipe to make
            (default: all of them)
        :returns: true if all packages were built correctly
        """
        recipe_dir = os.path.join(paths.RECIPE_DIR, recipe_name)
        build_dir = os.path.join(paths.WORK_DIR, recipe_name)
        recipe = Recipe.from_file(recipe_dir)

        context = {"recipe": recipe.name}
        adapter = BuildContextAdapter(logger, context)

        try:
            os.mkdir(build_dir)
        except FileExistsError:
            build_dir_rel = os.path.relpath(build_dir)
            ans = util.query_user(
                f"The build directory '{build_dir_rel}' for recipe \
'{recipe.name}' already exists.\nWould you like to [c]ancel, [r]emove that \
directory, or [k]eep it (not recommended)?",
                default="c",
                options=["c", "r", "k"],
                aliases={
                    "cancel": "c",
                    "remove": "r",
                    "keep": "k",
                },
            )

            if ans == "c":
                return False

            if ans == "r":
                shutil.rmtree(build_dir)
                os.mkdir(build_dir)

        src_dir = os.path.join(build_dir, "src")
        os.makedirs(src_dir, exist_ok=True)

        base_pkg_dir = os.path.join(build_dir, "pkg")
        os.makedirs(base_pkg_dir, exist_ok=True)

        self._fetch_source(adapter, recipe, recipe_dir, src_dir)
        self._prepare(adapter, recipe, src_dir)
        self._build(adapter, recipe, src_dir)
        self._strip(adapter, recipe, src_dir)

        for package_name in (
            packages_names
            if packages_names is not None
            else recipe.packages.keys()
        ):
            if package_name not in recipe.packages:
                raise BuildError(
                    f"Package '{package_name}' does not exist in \
recipe '{recipe.name}'"
                )

            assert package_name is not None
            package = recipe.packages[package_name]
            context["package"] = package_name

            pkg_dir = os.path.join(base_pkg_dir, package_name)
            os.makedirs(pkg_dir, exist_ok=True)

            self._package(adapter, package, src_dir, pkg_dir)
            self._archive(adapter, package, pkg_dir)

        return True

    def _fetch_source(
        self,
        adapter: BuildContextAdapter,
        recipe: Recipe,
        recipe_dir: str,
        src_dir: str,
    ) -> None:
        """Fetch and extract all source files required to build a recipe."""
        adapter.info("Fetching source files")

        for source in recipe.sources:
            filename = os.path.basename(source.url)
            local_path = os.path.join(src_dir, filename)

            if self.URL_REGEX.match(source.url) is None:
                # Get source file from the recipe’s directory
                shutil.copy2(os.path.join(recipe_dir, source.url), local_path)
            else:
                # Fetch source file from the network
                req = requests.get(source.url)

                if req.status_code != 200:
                    raise BuildError(
                        f"Unexpected status code while fetching \
source file '{source.url}', got {req.status_code}"
                    )

                with open(local_path, "wb") as local:
                    for chunk in req.iter_content(chunk_size=1024):
                        local.write(chunk)

            # Verify checksum
            if (
                source.checksum != "SKIP"
                and util.file_sha256(local_path) != source.checksum
            ):
                raise BuildError(
                    f"Invalid checksum for source file {source.url}"
                )

            # Automatically extract source archives
            if not source.noextract:
                util.auto_extract(local_path, src_dir)

    def _prepare(  # pylint: disable=no-self-use
        self, adapter: BuildContextAdapter, recipe: Recipe, src_dir: str
    ) -> None:
        """Prepare source files before building."""
        if not recipe.actions["prepare"]:
            adapter.info("Skipping prepare (nothing to do)")
            return

        adapter.info("Preparing source files")
        logs = bash.run_script(
            script=recipe.actions["prepare"],
            variables={**recipe.variables, "srcdir": src_dir},
        )

        for line in logs:
            adapter.debug(line)

    def _build(
        self, adapter: BuildContextAdapter, recipe: Recipe, src_dir: str
    ) -> None:
        """Build artifacts for a recipe."""
        if not recipe.actions["build"]:
            adapter.info("Skipping build (nothing to do)")
            return

        adapter.info("Building artifacts")
        mount_src = "/src"
        uid = os.getuid()

        logs = bash.run_script_in_container(
            self.docker,
            image=self.IMAGE_PREFIX + recipe.image,
            mounts=[
                docker.types.Mount(
                    type="bind",
                    source=os.path.abspath(src_dir),
                    target=mount_src,
                )
            ],
            variables={**recipe.variables, "srcdir": mount_src},
            script="\n".join(
                (
                    f'cd "{mount_src}"',
                    recipe.actions["build"],
                    f'chown -R {uid}:{uid} "{mount_src}"',
                )
            ),
        )

        for line in logs:
            adapter.debug(line)

    def _strip(
        self, adapter: BuildContextAdapter, recipe: Recipe, src_dir: str
    ) -> None:
        """Strip all debugging symbols from binaries."""
        if "nostrip" in recipe.flags:
            adapter.info("Not stripping binaries (nostrip flag set)")
            return

        adapter.info("Stripping binaries")
        mount_src = "/src"

        logs = bash.run_script_in_container(
            self.docker,
            image=self.IMAGE_PREFIX + self.DEFAULT_IMAGE,
            mounts=[
                docker.types.Mount(
                    type="bind",
                    source=os.path.abspath(src_dir),
                    target=mount_src,
                )
            ],
            variables={},
            script="\n".join(
                (
                    # Strip binaries in the target arch
                    f'find "{mount_src}" -type f -executable -print0 \
| xargs --no-run-if-empty --null "${{CROSS_COMPILE}}strip" --strip-all || true',
                    # Strip binaries in the host arch
                    f'find "{mount_src}" -type f -executable -print0 \
| xargs --no-run-if-empty --null strip --strip-all || true',
                )
            ),
        )

        for line in logs:
            adapter.debug(line)

    def _package(  # pylint: disable=no-self-use
        self,
        adapter: BuildContextAdapter,
        package: Package,
        src_dir: str,
        pkg_dir: str,
    ) -> None:
        """Make a package from a recipe’s build artifacts."""
        adapter.info("Packaging build artifacts")
        logs = bash.run_script(
            script=package.action,
            variables={
                **package.variables,
                "srcdir": src_dir,
                "pkgdir": pkg_dir,
            },
        )

        for line in logs:
            adapter.debug(line)

        adapter.debug("Resulting tree:")

        for filename in glob.iglob(pkg_dir + "/**/*", recursive=True):
            adapter.debug(" - %s", filename.removeprefix(pkg_dir))

    def _archive(
        self, adapter: BuildContextAdapter, package: Package, pkg_dir: str
    ) -> None:
        """Create an archive for a package."""
        adapter.info("Creating archive")
        ar_path = os.path.join(paths.REPO_DIR, package.filename())

        # Convert install scripts to Debian format
        scripts = {}
        script_header = "\n".join(
            (
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -e
                    """
                ),
                bash.put_variables(package.variables),
                self.install_lib,
            )
        )

        for name, script, action in (
            ("preinstall", "preinst", "install"),
            ("configure", "postinst", "configure"),
        ):
            if package.install[name]:
                scripts[script] = "\n".join(
                    (
                        script_header,
                        textwrap.dedent(
                            f"""\
                            if [[ $1 = {action} ]]; then
                                fun() {{
                            """
                        ),
                        package.install[name],
                        textwrap.dedent(
                            """\
                                }}
                                fun
                            fi
                            """
                        ),
                    )
                )

        for step in ("pre", "post"):
            if (
                package.install[step + "upgrade"]
                or package.install[step + "remove"]
            ):
                script = script_header

                for action in ("upgrade", "remove"):
                    if package.install[step + action]:
                        script += "\n".join(
                            (
                                textwrap.dedent(
                                    f"""\
                                    if [[ $1 = {action} ]]; then
                                        fun() {{
                                    """
                                ),
                                package.install[step + action],
                                textwrap.dedent(
                                    """\
                                        }}
                                        fun
                                    fi
                                    """
                                ),
                            )
                        )

                scripts[step + "rm"] = script

        adapter.debug("Install scripts:")

        if scripts:
            for script in scripts:
                adapter.debug(" - %s", script)
        else:
            adapter.debug("(none)")

        epoch = int(package.parent.timestamp.timestamp())

        with open(ar_path, "wb") as file:
            ipk.make_ipk(
                file,
                epoch=epoch,
                pkg_dir=pkg_dir,
                metadata=package.control_fields(),
                scripts=scripts,
            )

        # Set fixed mtime for the resulting archive
        os.utime(ar_path, (epoch, epoch))
