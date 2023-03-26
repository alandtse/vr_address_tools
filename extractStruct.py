#!/usr/bin/env python3
import aiofiles
import argparse
import asyncio
import io
import mmap
import os
import pcpp
import re
import CppHeaderParser
import pathlib
from typing import List, Optional
import locale

locale.setlocale(locale.LC_ALL, "")  # Use '' for auto, or force e.g. to 'en_US.UTF-8'

HEADER_TYPES = (".h", ".hpp", ".hxx")
SOURCE_TYPES = (".c", ".cpp", ".cxx")
ALL_TYPES = HEADER_TYPES + SOURCE_TYPES
IGNORED_NAMESPACES = ("std", "re", "stl", "skse", "rel", "skse::winapi")
REMOVED_NAMESPACES = ("re::", "winapi::", "::", "skse::winapi::", "std::")
SKIP_ITEMS = ("inline ", "volatile ", "T", "mutable ", "friend ", "return")
STD_TYPES = (
    "int8_t",
    "int16_t",
    "int32_t",
    "int64_t",
    "uint8_t",
    "uint16_t",
    "uint32_t",
    "uint64_t",
    "size_t",
)

debug = False
args = {}
SKYRIM_BASE = "0x140000000"
include_dirs = []
processedFiles = {}
defined = {}
undefined = {}
target_structs = ["classes", "enums", "global_enums"]
results = []


class preParser(pcpp.Preprocessor):
    def get_output(self) -> str:
        """Return this objects current tokens as a string."""
        with io.StringIO() as buffer:
            self.write(buffer)
            for name in self.known_defines:
                buffer.write(f"#define {name} ...\n")
            return buffer.getvalue()

    def on_include_not_found(
        self, is_malformed: bool, is_system_include: bool, curdir: str, includepath: str
    ) -> None:
        """Pass through bad includes."""
        raise pcpp.OutputDirective(pcpp.Action.IgnoreAndPassThrough)


async def preProcessData(data: str, defines=None) -> str:
    """Use pcpp to preprocess string.

    Args:
        data (str): Input data string
        defines (dict, optional): Dict of defines. Defaults to None.

    Returns:
        str: string after processing
    """

    defines = defines or {}
    global cpp
    for key, value in defines.items():
        cpp.define(f"{key} {value}")
    cpp.parse(data)
    with io.StringIO() as buffer:
        cpp.write(buffer)
        return buffer.getvalue()


def add_hex_strings(input1: str, input2: str = "0") -> str:
    """Return sum of two hex strings.

    Args:
        input1 (str): Hex formatted string.
        input2 (str, optional): Hex formatted string. Defaults to "0".

    Returns:
        str: Hex string sum.
    """
    if input1 is None:
        return ""
    if isinstance(input1, int):
        input1 = str(input1)
    if isinstance(input2, int):
        input2 = str(input2)
    return hex(int(input1, 16) + int(input2, 16))


async def walk_directories(
    a_directory: str,
    a_exclude: List[str],
) -> dict:
    """Scan code for uses of rel::id and also populate any known id_vr maps from offsets.

    Args:
        a_directory (str): Root directory to walk.
        a_exclude (List[str]): List of file names to ignore.

    Returns:
        results (Dict[str]): Dict of defined_rel_ids, defined_vr_offsets, results
    """
    global debug
    global processedFiles
    global include_dirs
    for dirpath, dirnames, filenames in os.walk(a_directory):
        rem = []
        for dirname in dirnames:
            if dirname in a_exclude:
                rem.append(dirname)
        for todo in rem:
            dirnames.remove(todo)
        if dirpath.lower().endswith("include"):
            include_dirs.append(dirpath)
        for filename in filenames:
            if filename not in a_exclude and filename.endswith(HEADER_TYPES):
                await cpp_header_parse(dirpath, filename)
    print(
        f"Finished scanning {len(processedFiles):n} files. Found {len(defined)} defines."
    )


def removeNamespaces(item: Optional[str]) -> Optional[str]:
    if item is None:
        return None

    for prefix in REMOVED_NAMESPACES:
        item = re.sub(f"(?:^|[ ,]){prefix}(\w+)", r"\1", item, flags=re.IGNORECASE)
    return item


def slugify(item: Optional[str]) -> Optional[str]:
    """Convert string to string Ghidra/IDA can process for structs.

    Args:
        item (str): input string for replacement

    Returns:
        str: output string with items replaced
    """
    if item is None:
        return None
    DEFAULT_REPLACE = "_"
    REPLACEMENTS = {
        # "int8_t": "int8",
        # "int16_t": "int16",
        # "int32_t": "int32",
        # "int64_t": "int64",
        # "uint8_t": "uint8",
        # "uint16_t": "uint16",
        # "uint32_t": "uint32",
        # "uint64_t": "uint64",
        # "size_t": "uint64",
        "UPInt": "uint64",
        "SPInt": "uint64",
        " ": "",
        "-": DEFAULT_REPLACE,
        "<": DEFAULT_REPLACE,
        ">": DEFAULT_REPLACE,
        ",": DEFAULT_REPLACE,
        ":": DEFAULT_REPLACE,
    }
    for k, v in REPLACEMENTS.items():
        item = item.replace(k, v)
    return item[:-1].replace("*", "") + item[-1] if item else item


def isDefined(namespace: str, name: str) -> bool:
    global defined
    index = f"{namespace}::{name}"
    index = index.replace("::::", "::")
    return index in defined


def isDefinedItem(item) -> bool:
    namespace = item["namespace"]
    if namespace and not namespace.endswith("::"):
        namespace = f"{item['namespace']}::"
    name = item.get("name", "anonymous")
    return isDefined(namespace, name)


def prepare_for_print(item, membersOnly=False):
    global results
    global undefined
    item_name = removeNamespaces(item.get("name"))
    item_namespace = removeNamespaces(item.get("namespace"))
    if item_namespace.lower() in IGNORED_NAMESPACES:
        item_namespace = ""
    item_name = slugify(item_name)
    item_namespace = (
        item_namespace + "::"
        if not item_namespace.endswith("::") and item_namespace
        else item_namespace
    )
    item_namespace = slugify(item_namespace)

    # enums
    count = item.get("count", 0)
    values = item.get("values")
    if values and not membersOnly:
        results.append(f"enum {item_name} {{\t//\t{count}\n")
        try:
            for enum in values:
                enum_n = enum.get("name")
                enum_v = add_hex_strings(enum.get("value"))
                results.append(f"\t{enum_n} = {enum_v},\n")
        except ValueError:
            pass
        results.append("};\n")

    # class members
    if not membersOnly:
        results.append(f"struct {item_namespace}{item_name}{{\t//\t{count}\n")
    parents = item.get("inherits", [])
    for parent in parents:
        class_name = parent["class"]
        namespace = item.get("namespace", "")
        parent_index = f"{namespace}::{class_name}"
        if isDefined(namespace, class_name):
            prepare_for_print(defined[parent_index], membersOnly=True)
    public_list = item.get("properties", {}).get("public", [])
    if public_list:
        for listItem in public_list:
            ctypes = listItem.get("ctypes_type")
            name = listItem.get("name")
            namespace = listItem.get("namespace")
            namespace = namespace + "::" if not namespace.endswith("::") else namespace
            type_ = listItem.get("raw_type", "")
            if type_.find("(") != -1 and type_.find(")") == -1:
                # catch bug in cppparser where parentheses not closed
                type_ = type_ + ")"
            if ignore_type(type_):
                continue
            if not isDefined(namespace, type_) and ctypes == "ctypes.c_void_p":
                undefined_index = slugify(removeNamespaces(type_))
                if undefined_index in undefined:
                    undefined[undefined_index] += 1
                else:
                    undefined[undefined_index] = 1
            if listItem.get("pointer"):
                type_ += "*"
            type_ = slugify(removeNamespaces(type_))
            name = removeNamespaces(name) if name else "anonymous"
            results.append(f"\t{type_}\t{name};\t\t\t// {item_name}\n")
    if not membersOnly:
        results.append("};\n")
    return results


def ignore_type(type_) -> bool:
    lower_type = type_.lower()
    for skip in SKIP_ITEMS:
        if (
            len(skip) > 1 and lower_type.startswith(skip)
        ) or lower_type.strip() == skip:
            return True
    return False


async def cpp_header_parse(dirpath, filename) -> bool:
    global include_dirs
    global defined
    global processedFiles

    def parse(item):
        namespace = item["namespace"]
        if namespace and not namespace.endswith("::"):
            namespace = f"{item['namespace']}::"
        name = item.get("name", "anonymous")
        index = f"{namespace}{name}"
        if isDefined(namespace, name):
            print(f"\tAlready defined {dirpath}/{filename}\t{index}")
            return
        print(f"\tParsing {dirpath}/{filename}\t{index}")
        defined[index] = item
        defined[index]["count"] = 0
        if name.rfind("::") > 1:
            direct_index = name[name.rfind("::") + 2 :]
            defined[direct_index] = defined[index]

    if processedFiles.get(f"{dirpath}/{filename}"):
        return True
    processedFiles[f"{dirpath}/{filename}"] = True
    result = False
    try:
        async with aiofiles.open(f"{dirpath}/{filename}", "r+") as f:
            data = mmap.mmap(f.fileno(), 0).read().decode("utf-8")
            processed_data = await preProcessData(data, {"SKYRIMVR": 1})
            # Next line solves bug https://github.com/robotpy/robotpy-cppheaderparser/issues/83
            processed_data = re.sub("\n#line .*", "\n", processed_data)
            await write("processed.h", processed_data)
            header = CppHeaderParser.CppHeader(
                processed_data,
                argType="string",
                preprocessed=True,
            )
    except (FileNotFoundError, ValueError):
        return True
    except CppHeaderParser.CppHeaderParser.CppParseError as ex:
        if debug:
            print(f"Unable to cppheaderparse {dirpath}/{filename}: ", ex)
        return True
    for include in header.includes:
        include = include[1:-1]
        for (
            dir
        ) in (
            include_dirs
        ):  # check include directories only since the include needs to search include paths
            path = pathlib.Path(dir, include)
            if path.is_file and not processedFiles.get(str(path)):
                await cpp_header_parse(str(path.parent), str(path.name))
    for struct in target_structs:
        items = getattr(header, struct)
        if getattr(header, struct):
            print(f"Processing {struct}: {len(items)}")
            if isinstance(items, list):
                for item in items:
                    parse(item)
            elif isinstance(items, dict):
                for key, item in items.items():
                    parse(item)
    return True


async def write_structs(output: str = "types.h") -> bool:
    global results
    global defined
    global undefined

    sortedlist = sorted(defined.values(), key=lambda kv: kv["count"], reverse=True)
    for v in sortedlist:
        prepare_for_print(v)
    undefined_string = ""
    for undefined, count in sorted(undefined.items(), key=lambda x: x[1], reverse=True):
        if undefined.lower() in STD_TYPES:
            continue
        undefined_string = f"{undefined_string}struct {undefined}{{}};\t//\t{count}\n"
    results.insert(0, undefined_string)
    return await write(output, results)


async def write(output: str = "types.h", data: str = "") -> bool:
    try:
        async with aiofiles.open(output, "w", newline="") as f:
            await f.writelines(data)
        return True
    except OSError as ex:
        print(f"Error writing to {output}: {ex}")
        return False


async def main():
    global debug
    global args
    global cpp
    parser = argparse.ArgumentParser(
        description="Parses Commonlib to create types.h for import into ghidra"
    )

    def dir_path(path):
        if os.path.isdir(path) or os.path.isfile:
            return path
        else:
            raise argparse.ArgumentTypeError(f"readable_dir:{path} is not a valid path")

    parser.add_argument("path", help="Path to the input directory.", type=dir_path)

    parser.add_argument(
        "-d",
        "--debug",
        action="store_true",
        help="Print debug messages.",
    )
    args = vars(parser.parse_args())
    debug = args.get("debug")

    if debug:
        print(args)
    exclude = ["build", "buildvr", "extern", "external"]
    scan_results = {}
    cpp = preParser()  # init preprocessor
    # Load files from location of python script

    if args["path"]:
        root = args["path"]
        if os.path.isdir(root):
            os.chdir(root)
    else:
        root = os.path.split(os.path.dirname(os.path.realpath(__file__)))[0]
        os.chdir(root)
    path = pathlib.Path(root)
    if path.is_dir():
        scan_results = await walk_directories(
            root,
            exclude,
        )
    elif path.is_file():
        parent = path.parent
        while str(parent) != ".":
            if str(parent.name).lower() == "include":
                include_dirs.append(str(parent))
            parent = parent.parent
        await cpp_header_parse(str(path.parent), str(path.name))
    await write_structs()


if __name__ == "__main__":
    asyncio.run(main())
