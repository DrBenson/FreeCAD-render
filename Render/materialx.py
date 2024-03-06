# ***************************************************************************
# *                                                                         *
# *   Copyright (c) 2024 Howefuft <howetuft-at-gmail>                       *
# *                                                                         *
# *   This program is free software; you can redistribute it and/or modify  *
# *   it under the terms of the GNU Lesser General Public License (LGPL)    *
# *   as published by the Free Software Foundation; either version 2 of     *
# *   the License, or (at your option) any later version.                   *
# *   for detail see the LICENCE text file.                                 *
# *                                                                         *
# *   This program is distributed in the hope that it will be useful,       *
# *   but WITHOUT ANY WARRANTY; without even the implied warranty of        *
# *   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the         *
# *   GNU Library General Public License for more details.                  *
# *                                                                         *
# *   You should have received a copy of the GNU Library General Public     *
# *   License along with this program; if not, write to the Free Software   *
# *   Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  *
# *   USA                                                                   *
# *                                                                         *
# ***************************************************************************

"""This module provides features to import MaterialX materials in Render WB."""

# TODO list
# Solve scale question
# Add Scale to Disney displacement (and to renderers...)
# Add icons
# Handle case when no MaterialX system installed
# Reorganize code: rename, create a subdir
# Write the doc
# Remove downloaded zip
# Explicit doc
# Fix texture dimensions
# Solve TODO: explicit doc etc.
# Handle HDR (set basetype to FLOAT, see translateshader.py)


import zipfile
import tempfile
import os
import subprocess
import shutil
import threading

try:
    import MaterialX as mx
    from MaterialX import PyMaterialXGenShader as mx_gen_shader
    from MaterialX import PyMaterialXRender as mx_render
except (ModuleNotFoundError, ImportError):
    MATERIALX = False
else:
    MATERIALX = True
    from Render.materialx_baker import RenderTextureBaker, MaterialXInterrupted

import FreeCAD as App

import Render.material
from Render.constants import MATERIALXDIR


class MaterialXImporter:
    """A class to import a MaterialX material into a RenderMaterial."""

    def __init__(self, filename, doc=None, progress_hook=None):
        self._filename = filename
        self._doc = doc or App.ActiveDocument
        self._baker_ready = threading.Event()
        self._request_halt = threading.Event()
        self._progress_hook = progress_hook

        self._working_dir = ""  # Working directory
        self._mtlx_filename = ""  # Initial MaterialX file name
        self._search_path = None
        self._translated = None  # Translated document
        self._baker = None  # Baker
        self._baked = None  # Baked document

    def run(self):
        """Import a MaterialX archive as Render material."""
        # MaterialX system available?
        if not MATERIALX:
            _warn("Missing MaterialX library: unable to import material")
            return

        # Proceed with file
        with tempfile.TemporaryDirectory() as working_dir:
            print("STARTING MATERIALX IMPORT")
            try:
                # Prepare
                self._working_dir = working_dir
                self._unzip_files()
                self._compute_search_path()

                # Translate, bake and convert to render material
                self._translate_materialx()
                self._prepare_baker()
                self._bake_materialx()
                self._make_render_material()

            except MaterialXInterrupted:
                print("IMPORT - INTERRUPTED")
            except MaterialXError as error:
                print(f"IMPORT - ERROR ('{error.message}')")
            else:
                print("IMPORT - SUCCESS")

    def cancel(self):
        """Request process to halt.

        This command is designed to be executed in another thread than run.
        """
        self._request_halt.set()
        if self._baker_ready.is_set():
            self._baker.request_halt()

    def canceled(self):
        """Check if halt has been requested."""
        return self._request_halt.is_set()

    def _check_halt_requested(self):
        """Check if halt is requested, raise MaterialXInterrupted if so."""
        if self._request_halt.is_set():
            raise MaterialXInterrupted()

    # Helpers
    def _set_progress(self, value, maximum):
        """Report progress."""
        if self._progress_hook is not None:
            self._progress_hook(value, maximum)

    def _unzip_files(self):
        """Unzip materialx package, if needed.

        This method also set self._mtlx_filename
        """
        if zipfile.is_zipfile(self._filename):
            if self._request_halt.is_set():
                raise MaterialXInterrupted()
            with zipfile.ZipFile(self._filename, "r") as matzip:
                # Unzip material
                print(f"Extracting to {self._working_dir}")
                matzip.extractall(path=self._working_dir)
                # Find materialx file
                files = (
                    entry.path
                    for entry in os.scandir(self._working_dir)
                    if entry.is_file() and entry.name.endswith(".mtlx")
                )
                try:
                    self._mtlx_filename = next(files)
                except StopIteration as exc:
                    raise MaterialXError("Missing mtlx file") from exc
        else:
            self._mtlx_filename = self._filename
        self._check_halt_requested()

    def _compute_search_path(self):
        """Compute search path for MaterialX."""
        assert self._working_dir
        assert self._mtlx_filename

        working_dir = self._working_dir
        mtlx_filename = self._mtlx_filename

        self._search_path = mx.getDefaultDataSearchPath()
        self._search_path.append(working_dir)
        self._search_path.append(os.path.dirname(mtlx_filename))
        self._search_path.append(MATERIALXDIR)

    def _translate_materialx(self):
        """Translate MaterialX from StandardSurface to RenderPBR.

        Args:
            matdir -- The directory where to find MaterialX files
        """
        assert self._mtlx_filename
        assert self._search_path

        mtlx_filename = self._mtlx_filename
        search_path = self._search_path

        # Read doc
        mxdoc = mx.createDocument()
        mx.readFromXmlFile(mxdoc, mtlx_filename)

        # Check material unicity and get its name
        if not (mxmats := mxdoc.getMaterialNodes()):
            raise MaterialXError("No material in file")
        if len(mxmats) > 1:
            raise MaterialXError(f"Too many materials ({len(mxmats)}) in file")
        mxmat = mxmats[0]

        # Clean doc for translation
        # Add own node graph
        if not (render_ng := mxdoc.getNodeGraph("RENDER_NG")):
            render_ng = mxdoc.addNodeGraph("RENDER_NG")

        # Move every cluttered root node to node graph
        exclude = {
            "nodedef",
            "nodegraph",
            "standard_surface",
            "surfacematerial",
            "displacement",
        }
        rootnodes = (
            n for n in mxdoc.getNodes() if n.getCategory() not in exclude
        )
        moved_nodes = set()
        for node in rootnodes:
            nodecategory = node.getCategory()
            nodename = node.getName()
            nodetype = node.getType()
            try:
                newnode = render_ng.addNode(
                    nodecategory,
                    nodename + "_",
                    nodetype,
                )
            except LookupError:
                # Already exist
                pass
            else:
                newnode.copyContentFrom(node)
                mxdoc.removeNode(nodename)
                newnode.setName(nodename)
                moved_nodes.add(nodename)

        # Connect shader inputs to node graph
        shader_inputs = (
            si
            for shader in mx.getShaderNodes(materialNode=mxmat, nodeType="")
            for si in shader.getInputs()
            if not si.hasValueString() and not si.getConnectedOutput()
        )
        for shader_input in shader_inputs:
            if (nodename := shader_input.getNodeName()) in moved_nodes:
                # Create output node in node graph
                newoutputname = f"{nodename}_output"
                try:
                    newoutput = render_ng.addOutput(
                        name=newoutputname,
                        type=render_ng.getNode(nodename).getType(),
                    )
                except LookupError:
                    pass
                else:
                    newoutput.setNodeName(nodename)

                # Connect input to output node
                shader_input.setOutputString(newoutputname)
                shader_input.setNodeGraphString("RENDER_NG")
                shader_input.removeAttribute("nodename")

        # Import libraries
        mxlib = mx.createDocument()
        library_folders = mx.getDefaultDataLibraryFolders()
        library_folders.append("render_libraries")
        mx.loadLibraries(library_folders, search_path, mxlib)
        mxdoc.importLibrary(mxlib)

        # Translate surface shader
        translator = mx_gen_shader.ShaderTranslator.create()
        try:
            translator.translateAllMaterials(mxdoc, "render_pbr")
        except mx.Exception as err:
            raise MaterialXError(
                "Translation error for surface shader"
            ) from err

        # Translate displacement shader
        dispnodes = [
            s
            for r in mx_gen_shader.findRenderableMaterialNodes(mxdoc)
            for s in mx.getShaderNodes(r, mx.DISPLACEMENT_SHADER_TYPE_STRING)
        ]
        try:
            for dispnode in dispnodes:
                translator.translateShader(dispnode, "render_disp")
        except mx.Exception as err:
            raise MaterialXError(
                "Translation error for displacement shader"
            ) from err

        self._translated = mxdoc

    def _prepare_baker(self):
        """Bake MaterialX material."""
        assert self._search_path
        assert self._translated

        search_path = self._search_path
        mxdoc = self._translated

        # Check the document for a UDIM set.
        udim_set_value = mxdoc.getGeomPropValue(mx.UDIM_SET_PROPERTY)
        udim_set = udim_set_value.getData() if udim_set_value else []

        # Compute baking resolution from the source document
        image_handler = mx_render.ImageHandler.create(
            mx_render.StbImageLoader.create()
        )
        image_handler.setSearchPath(search_path)
        if udim_set:
            resolver = mxdoc.createStringResolver()
            resolver.setUdimString(udim_set[0])
            image_handler.setFilenameResolver(resolver)
        image_vec = image_handler.getReferencedImages(mxdoc)
        bake_width, bake_height = mx_render.getMaxDimensions(image_vec)
        bake_width = max(bake_width, 4)
        bake_height = max(bake_height, 4)

        # Prepare baker
        self._baker = RenderTextureBaker(
            bake_width,
            bake_height,
            mx_render.BaseType.UINT8,
        )
        self._baker.setup_unit_system(mxdoc)
        self._baker.optimize_constants = True
        self._baker.hash_image_names = False
        self._baker.progress_hook = self._progress_hook

        self._baker_ready.set()

    def _bake_materialx(self):
        """Bake MaterialX material."""
        assert self._working_dir
        assert self._baker
        assert self._translated
        assert self._search_path

        output_dir = self._working_dir
        baker = self._baker
        mxdoc = self._translated
        search_path = self._search_path

        # Bake and retrieve
        _, outfile = tempfile.mkstemp(
            suffix=".mtlx", dir=output_dir, text=True
        )
        baker.bake_all_materials(mxdoc, search_path, outfile)

        mxdoc = mx.createDocument()
        mx.readFromXmlFile(mxdoc, outfile)

        # Validate document
        valid, msg = mxdoc.validate()
        if not valid:
            msg = f"Validation warnings for input document: {msg}"
            _warn(msg)

        self._baked = mxdoc

    def _make_render_material(self):
        """Make a RenderMaterial from a MaterialX baked material."""
        assert self._baked
        assert self._doc

        mxdoc = self._baked
        fcdoc = self._doc
        # Get PBR material
        # TODO Make it more predictable (name node_graph etc.)
        mxmats = mxdoc.getMaterialNodes()
        assert len(mxmats) == 1, f"len(mxmats) = {len(mxmats)}"
        mxmat = mxmats[0]
        mxname = mxmat.getAttribute("original_name")

        # Get images
        # TODO Make it more predictable (name node_graph etc.)
        node_graphs = mxdoc.getNodeGraphs()
        assert len(node_graphs) <= 1, f"len(node_graphs) = {len(node_graphs)}"
        if len(node_graphs):
            node_graph = node_graphs[0]
            images = {
                node.getName(): node.getInputValue("file")
                for node in node_graph.getNodes()
                if node.getCategory() == "image"
            }
            outputs = {
                node.getName(): node.getNodeName()
                for node in node_graph.getOutputs()
            }
        else:
            images = {}
            outputs = {}

        # Reminder: Material.Material is not updatable in-place (FreeCAD
        # bug), thus we have to copy/replace
        mat = Render.material.make_material(mxname)
        matdict = mat.Material.copy()
        matdict["Render.Type"] = "Disney"

        # Add textures, if necessary
        texture = None
        textures = {}
        for name, img in images.items():
            if not texture:
                texture, _, _ = mat.Proxy.add_texture(img)
                propname = "Image"
            else:
                propname = texture.add_image(imagename="Image", imagepath=img)
            textures[name] = propname
        texname = texture.fpo.Name if texture else None

        # Fill fields
        render_params = (
            param
            for node in mxdoc.getNodes()
            for param in node.getInputs()
            if node.getCategory() in ("render_pbr", "render_disp")
        )
        for param in render_params:
            if param.hasOutputString():
                # Texture
                output = param.getOutputString()
                image = textures[outputs[output]]
                name = param.getName()
                key = f"Render.Disney.{name}"
                if name != "Normal":
                    matdict[key] = f"Texture;('{texname}','{image}')"
                else:
                    matdict[key] = f"Texture;('{texname}','{image}', '1.0')"
            elif name := param.getName():
                # Value
                key = f"Render.Disney.{name}"
                matdict[key] = param.getValueString()
            else:
                msg = f"Unhandled param: '{name}'"
                _msg(msg)

        # Replace Material.Material
        mat.Material = matdict


def import_materialx(filename):
    """Import MaterialX (function version)."""
    if not MATERIALX:
        QMessageBox.critical(
            Gui.getMainWindow(),
            "MaterialX Import",
            "Error: Cannot find MaterialX framework!\n"
            "Please check MaterialX is correctly installed on your system "
            "before using this feature...",
        )
        return

    importer = MaterialXImporter(filename)
    importer.run()


class MaterialXError(Exception):
    """Exception to be raised when import encounters an error."""

    def __init__(self, msg):
        super().__init__()
        self.message = str(msg)


# Debug functions


def _print_doc(mxdoc):
    """Print a doc in XML format (debugging purposes)."""
    as_string = mx.writeToXmlString(mxdoc)
    for line in as_string.splitlines():
        print(line)


def _print_file(outfile):
    """Print a doc in XML format (debugging purposes)."""
    with open(outfile, encoding="utf-8") as f:
        for line in f:
            print(line, end="")


def _write_temp_doc(mxdoc):
    """Write a MX document to a temporary file."""
    _, outfile = tempfile.mkstemp(suffix=".mtlx", text=True)
    mx.writeToXmlFile(mxdoc, outfile)
    return outfile


def _run_materialx(outfile, tool="MaterialXView"):
    """Run MaterialX on outfile (debug purpose)."""
    tool = str(tool)
    assert tool in ["MaterialXView", "MaterialXGraphEditor"]
    args = [
        tool,
        "--material",
        outfile,
        "--path",
        MATERIALXDIR,
        "--library",
        "render_libraries",
    ]
    print(args)
    subprocess.run(args, check=False)


def _save_intermediate(outfile):
    """Save intermediate material (debug purpose)."""
    src = os.path.dirname(outfile)
    folder = os.path.basename(src)
    dest = os.path.join(App.getUserCachePath(), folder)
    print(f"Copying '{src}' into '{dest}'")
    shutil.copytree(src, dest)


def _warn(msg):
    """Emit warning during MaterialX processing."""
    App.Console.PrintWarning("[Render][MaterialX] " + msg)


def _msg(msg):
    """Emit warning during MaterialX processing."""
    App.Console.PrintMessage("[Render][MaterialX] " + msg)


def _view_doc(doc):
    """Open copy of doc in editor."""
    outfile = _write_temp_doc(doc)
    subprocess.run(["/usr/bin/nvim", outfile], check=False)
