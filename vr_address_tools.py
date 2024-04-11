#!/usr/bin/env python3
import aiofiles
import aiocsv
import argparse
import asyncio
import fileinput
import io
import mmap
import os
import re
import pcpp
import CppHeaderParser
from typing import List
import locale
import orjson

locale.setlocale(locale.LC_ALL, "")  # Use '' for auto, or force e.g. to 'en_US.UTF-8'

HEADER_TYPES = (".h", ".hpp", ".hxx")
SOURCE_TYPES = (".c", ".cpp", ".cxx")
ALL_TYPES = HEADER_TYPES + SOURCE_TYPES
PATTERN = r"rel::id\([^)]+\)"
PATTERN_GROUPS = r"rel::id(?:\s*(?P<name>\w*))(?:\(|{)\s*(?:(?P<id_with_offset>[0-9]+)[^)}]*(?P<sse_offset>0x[0-9a-f]*)|(?P<id>[0-9]*))\s*(?:\)|})"
# old rel:id pattern rel::id(514167)
RELID_PATTERN = r"(\w+){ REL::ID\(([0-9]+)\),*\s*([a-fx0-9])*\s+};"
# po3 latest pattern RELOCATION_ID(SSE, AE) and REL_ID(SSE, AE, VR)
# also	stl::write_thunk_call<MainLoop_Update>(REL::RelocationID(35551, 36550).address() + REL::Relocate(0x11F, 0x11F));
# also  variantid pattern static REL::Relocation<uintptr_t> func{ REL::VariantID(63017, 63942, 0xB40550) }; // B05710, B2A980, B40550  hkbBehaviorGraph::unk
RELOCATION_ID_PATTERN = r"(?P<prefix>[\w_]+)?(?:[{>(]* ?)?(?:rel::)?(?:REL(?:OCATION)?_?ID|VariantID)\((?P<sse>[0-9]+),+\s*(?P<ae>[0-9]*)(?:,+\s*0x(?P<vr_idoffset>[a-f0-9]*))?\)(?:,\s*OFFSET(?:_3)?\((?P<sse_offset>0x[a-f0-9]+)(?P<ae_offset>,\s*0x[a-f0-9]+)?(?P<vr_offset>,\s*0x[a-f0-9]+)?\))?(?:\s*};)?"
# tossaponk rel:id https://github.com/tossaponk/ArcheryLocationalDamage/blob/master/src/Offsets.h
TOSSPONK_REL_ID_PATTTERN = r"case (?P<func_name>[\w_]+)?:[^)]*rel::id\((?P<sseid>[0-9]+)\),\s*(?P<sse_offset>0x[0-9a-f]*)[^;]*rel::id\((?P<aeid>[0-9]+)\),\s*(?P<ae_offset>0x[0-9a-f]*)"
# commonlibsse-ng patterns constexpr REL::VariantID NiRTTI_BGSAddonNodeSoundHandleExtra(514633, 400793, 0x2f8a838);
VARIANT_ID_PATTERN = r"REL::VariantID\s+(?P<prefix>\w+)\((?P<sse>[0-9]+),+\s*(?P<ae>[0-9]*),+\s*0x(?P<vr_offset>[a-f0-9]*)\);"
# ersh variantID
# regex = REL::VariantID\s*(?P<prefix>\w+)?\((?P<sse>[0-9]+),+\s*(?P<ae>[0-9]*),+\s*0x(?P<vr_offset>[a-f0-9]*)\)
# static REL::Relocation<uintptr_t> func{ REL::VariantID(63017, 63942, 0xB40550) }; // B05710, B2A980, B40550  hkbBehaviorGraph::unk
# Maxsu
# NodeArray& init_withNode_withname(NodeArray& array, const char* name, CombatBehaviorTreeNode* node)
# {
# 	return _generic_foo<46261, NodeArray&, NodeArray&, const char*, CombatBehaviorTreeNode*>(array, name, node);
# }
GENERIC_FOO_ID = r"_generic_foo<(?P<sse>[0-9]+),"
# DKUtil IDToAbs, DKUtil uses alphabetical order so AE ID goes first DKUtil::Hook::IDToAbs(50643, 49716)
DKUTIL_ID_TO_ABS_PATTERN = r"DKUtil::Hook::IDToAbs\((?P<ae>[0-9]+),+\s*(?P<sse>[0-9]*)(?:,+\s*0x(?P<vr_idoffset>[a-f0-9]*))?\)"
## These are regexes for parsing offset files that typically can help define relationships (older commonlibvr); po3 and ng now allow for definition through macro use
# commonlibsse-ng patterns
# namespace BSSoundHandle
# 	{
# 		constexpr auto IsValid = RELOCATION_ID(66360, 67621);
OFFSET_PATTERN_RELOCATION_ID = r"constexpr auto (?P<name>\w*)\s*=\s*REL(?:OCATION)?_ID\((?P<sse>[0-9]+),+\s*(?P<ae>[0-9]*)\)"
OFFSET_PATTERN = r"(\w+){ REL::Offset\(([a-fx0-9]+)\)\s+};"
OFFSET_RELID_PATTERN = (
    r"(?:inline|constexpr) REL::ID\s+(\w+)\s*(?:\(|\{)\s*([a-fx0-9]+)"
)
OFFSET_VTABLE_RELID_PATTERN = r"(?:(?P<name>\w+){\s*|(?:\\g<name>{ *\\g<relid> , )*)(?P<relid>rel::id\((?:([0-9]+)[^)]*(0x[0-9a-f]*)|([0-9]+))\)*)+"
OFFSET_VTABLE_OFFSET_PATTERN = r"(?:(?P<name>\w+){\s*|(?:\\g<name>{ *\\g<reloffset> , )*)(?P<reloffset>rel::offset\((?:([a-fx0-9]+)[^)]*)\)*)+"
OFFSET_OFFSET_PATTERN = (
    r"(?:inline|constexpr) REL::Offset\s+(\w+)\s*(?:\(|\{)\s*([a-fx0-9]+)"
)
IFNDEF_PATTERN = r"([\w():]*)\s*{\s*#ifndef SKYRIMVR\s*([^{]*){\s*rel::id\(([0-9]*)\)\s}.*\s*#else\s*\2{.*(?:rel::offset)*(0x[0-9a-f]*)"
RELID_MATCH_ARRAY = [
    PATTERN_GROUPS,
    RELOCATION_ID_PATTERN,
    GENERIC_FOO_ID,
    DKUTIL_ID_TO_ABS_PATTERN,
]
REL_ID_VTABLE = "rel::id vtable"
REL_OFFSET_VTABLE = "rel::offset vtable"
REL_ID = "rel::id"
REL_OFFSET = "rel::offset"
REGEX_PARSE_DICT = {
    REL_ID_VTABLE: OFFSET_VTABLE_RELID_PATTERN,
    REL_OFFSET_VTABLE: OFFSET_VTABLE_OFFSET_PATTERN,
    REL_ID: OFFSET_RELID_PATTERN,
    REL_OFFSET: OFFSET_OFFSET_PATTERN,
}
FUNCTION_REGEX = r"(?:class (?P<class_decl>\w+)[&\w\s;:<>{=[\]*]*?)?(?P<return_type>[\w<>:*]+)\s+(?:\w+::)?(?P<func_name>[\w]+)\s*\((?P<args>[^)]*),?\s*\)[\w\s]*{(?:[\w\s=]*decltype\(&(?P<class>\w+)::(?P=func_name)+(?:<.*>)?\))?[&\w\s;:<>{=*]*REL(?:[\w:]*ID)\((?:(?P<id>\d*)|(?P<sseid>\d*),\s*(?P<aeid>\d*))\) };"
GENERIC_FOO_REGEX = r"(?P<return_type>[\w<>:*&]+)\s+(?:\w+::)?(?P<func_name>[\w]+)\s*\((?P<args>[^)]*)?\s*\)[\w\s]*{[&\w\s;:<>{=*/+-.]*_generic_foo<(?:(?P<id>\d*)),\s+(?P=return_type)(?:,\s*)?(?:(?P<class>\w+)\*)?.*>\(.*\);"
ARGS_REGEX = r"(?P<arg_pair>(?:const )?(?P<arg_type>[\w*&:_]+)\s+(?P<arg>[\w_]*)),?"
FUNCTION_REGEX_PARSE_DICT = {
    "decltype": FUNCTION_REGEX,
    "tossponk": TOSSPONK_REL_ID_PATTTERN,
    "generic_foo": GENERIC_FOO_REGEX,
}
REPLACEMENT = """
#ifndef SKYRIMVR
	{}  // SSE {}
#else
	{}  // TODO: VERIFY {}
#endif
"""
id_sse = {}
id_name = {}  # id to name database
id_vr = {}
sse_vr = {}
sse_ae = {}
ae_name = {}
offset_name = {}
id_vr_status = {}
debug = False
args = {}
SKYRIM_BASE = "0x140000000"
PDB_BASE = {
    # "skyrim": {1: "0x140001000", 2: "0x141580290", 3: "0x141DA5570"},
    "fallout": {1: "0x140001000", 2: "0x142c0c000", 3: "0x142C17000"},
}
CONFIDENCE = {
    "UNKNOWN": 0,  # ID is unknown
    "SUGGESTED": 1,  # At least one automated database matched
    "MANUAL": 2,  # One person has confirmed a match
    "VERIFIED": 3,  # Manual + Suggested
    "PERFECT": 4,  # Bit by bit match
}


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
    if input1 is None or input1 == "":
        return ""
    if isinstance(input1, int):
        input1 = str(input1)
    if isinstance(input2, int):
        input2 = str(input2)
    return hex(int(input1, 16) + int(input2, 16))


async def load_database(
    addresslib="addrlib.csv",
    offsets="offsets-1.5.97.0.csv",
    ida_compare="sse_vr.csv",
    ida_override=True,
    se_ae="se_ae.csv",
    ae_names="AddressLibraryDatabase/skyrimae.rename",
    se_ae_offsets="se_ae_offsets.csv",
    skyrim=True,
    pdb_json="pdb.json",
) -> int:
    """Load databases.

    Args:
        addresslib (str, optional): Name of csv with VR Address, SSE Address, ID (e.g., 0x1400010d0,0x1400010d0,2). Defaults to "addrlib.csv".
        offsets (str, optional): Name of csv with ID, SSE Address (e.g., 2,10d0). SSE Address is an offset that needs to be added to a base and is dumped from Address Library. Defaults to "offsets-1.5.97.0.csv".
        ida_compare (str, optional): Name of IDADiffCalculator csv with SSE Address, VR Address (e.g., 0x141992C10,0x141A33D38). Defaults to "sse_vr.csv".
        ida_override (bool, optional): Whether IDADiffCalculator will override offsets.
        se_ae (str, optional): Name of sse to ae ID mapping csv (e.g., sseid,aeid,confidence,name). Defaults to "se_ae.csv".
        ae_names (str, optional): Name of ae ID to name mapping (e.g., 11 MonitorAPO::Func9_*). Defaults to "AddressLibraryDatabase/skyrimae.rename".
        se_ae_offsets (str, optional): Name of merged sse/ae id/address map. Based off meh's mapping offsets, https://www.nexusmods.com/skyrimspecialedition/mods/32444?tab=files, and AddressLibraryDatabase comments. Created using merge.py. Defaults to "se_ae_offsets.csv".
        skyrim (bool,optional): Whether analyzing skyrim or fallout4. Defaults to True
        pdb_json (str, optional): Name of PDB converted to yaml to parse. Defaults to pdb.yaml. This should be the PublicsStream.
    Returns:
        int: Number of successfully loaded csv files. 0 means none were loaded.
    """
    loaded = 0
    global id_sse
    global id_vr
    global id_vr_status
    global debug
    path = os.path.realpath(os.path.join(os.getcwd(), os.path.dirname(__file__)))
    database_path = (
        "skyrim_vr_address_library" if skyrim else "fallout_vr_address_library"
    )
    database = "database.csv" if skyrim else "fo4_database.csv"
    path = os.path.join(path, database_path)
    try:
        async with aiofiles.open(os.path.join(path, database), mode="r") as infile:
            reader = aiocsv.AsyncDictReader(infile, restval="")
            async for row in reader:
                id = int(row["id"])
                sse = add_hex_strings(row["sse" if skyrim else "fo4"])
                vr = add_hex_strings(row["vr"])
                id_sse[id] = sse
                id_vr[id] = vr
                id_vr_status[id] = {
                    "sse": sse,
                    "name": row["name"],
                    "status": row["status"],
                }
                loaded += 1
    except FileNotFoundError:
        print(f"database.csv not found")

    try:
        async with aiofiles.open(os.path.join(path, addresslib), mode="r") as infile:
            reader = aiocsv.AsyncDictReader(infile)
            async for row in reader:
                id = int(row["id"])
                sse = add_hex_strings(row.get("sse" if skyrim else "fo4_addr"))
                vr = add_hex_strings(row.get("vr" if skyrim else "vr_addr"))
                if id_vr_status.get(id):
                    if debug:
                        print(
                            f"Database Load Warning: {id} already loaded skipping load from {addresslib}"
                        )
                elif vr:
                    id_sse[id] = sse
                    id_vr[id] = vr
                    id_vr_status[id] = {
                        "sse": sse,
                        "status": CONFIDENCE["SUGGESTED"],
                    }
                    loaded += 1
    except FileNotFoundError:
        print(f"{addresslib} not found")

    try:
        async with aiofiles.open(os.path.join(path, offsets), mode="r") as infile:
            reader = aiocsv.AsyncDictReader(infile)
            async for row in reader:
                id = int(row["id"])
                sse = add_hex_strings(
                    f"0x{row['sse' if skyrim else 'fo4_addr']}", SKYRIM_BASE
                )
                if id_sse.get(id) and id_sse.get(id) != sse:
                    print(
                        f"Database Load Warning: {id} mismatch {sse}	{id_sse.get(id)}"
                    )
                elif id_sse.get(id) is None:
                    id_sse[id] = sse
                loaded += 1
    except FileNotFoundError:
        print(f"{offsets} not found")
    try:
        async with aiofiles.open(os.path.join(path, ida_compare), mode="r") as infile:
            reader = aiocsv.AsyncDictReader(infile)
            async for row in reader:
                if skyrim:
                    sse = add_hex_strings(row["sse"])
                    vr = add_hex_strings(row["vr"])
                else:
                    sse = add_hex_strings(row["fo4_addr"], SKYRIM_BASE)
                    vr = add_hex_strings(row["vr_addr"], SKYRIM_BASE)
                sse_vr[sse] = vr
                loaded += 1
    except FileNotFoundError:
        print(f"{ida_compare} not found")
    try:
        with open(os.path.join(path, pdb_json), mode="r") as infile:
            pdb = orjson.loads(infile.read())
            for record in pdb["PublicsStream"]["Records"]:
                name = record["PublicSym32"]["Name"]
                offset = record["PublicSym32"]["Offset"]
                segment = record["PublicSym32"]["Segment"]
                found_base = PDB_BASE.get("skyrim" if skyrim else "fallout", {}).get(
                    segment
                )
                if found_base:
                    offset_name[add_hex_strings(hex(offset), found_base)] = name
            if debug:
                print(f"{pdb_json} loaded with {len(offset_name)} entries")
    except FileNotFoundError:
        print(f"{pdb_json} not found")
    if skyrim:
        try:
            async with aiofiles.open(os.path.join(path, se_ae), mode="r") as infile:
                reader = aiocsv.AsyncDictReader(infile)
                # sseid,aeid,confidence,name
                async for row in reader:
                    sseid = int(row["sseid"])
                    aeid = int(row["aeid"])
                    confidence = int(row["confidence"])
                    name = row["name"]
                    sse_ae[sseid] = aeid
                    ae_name[aeid] = name
        except FileNotFoundError:
            print(f"{se_ae} not found")

        try:
            async with aiofiles.open(os.path.join(path, ae_names), mode="r") as infile:
                reader = aiocsv.AsyncReader(infile, delimiter=" ")
                # 11 MonitorAPO::Func9_*
                async for row in reader:
                    if len(row) < 2:
                        continue
                    aeid = int(row[0])
                    name = row[1]
                    if aeid and name and not ae_name.get(aeid):
                        # print(
                        #     f"Adding name ae {aeid} {ae_name.get(aeid)} with {name}"
                        # )
                        ae_name[aeid] = name
        except FileNotFoundError:
            print(f"{ae_names} not found")

        try:
            async with aiofiles.open(
                os.path.join(path, se_ae_offsets), mode="r"
            ) as infile:
                reader = aiocsv.AsyncDictReader(infile, delimiter=",")
                # sseid,sse_addr,ae_addr,aeid,comments
                async for row in reader:
                    sseid = int(row["sseid"])
                    aeid = int(float(row["aeid"])) if row["aeid"] else 0
                    name = row["comments"]
                    if sseid and aeid:
                        sse_ae[sseid] = aeid
                    if aeid and name and not ae_name.get(aeid):
                        # print(
                        #     f"Adding name ae {aeid} {ae_name.get(aeid)} with {name}"
                        # )
                        ae_name[aeid] = name
        except FileNotFoundError:
            print(f"{se_ae_offsets} not found")
    if debug:
        print("Combining databases")
    conflicts = 0
    ids = 0
    for id, sse_addr in id_sse.items():
        ids += 1
        ida_addr = add_hex_strings(sse_vr.get(sse_addr))
        if id_vr_status.get(id):
            if debug:
                print(
                    f"Database Load Warning: {id} loaded by {database}; skipping IDA check"
                )
        elif id_vr.get(id) and ida_addr and id_vr.get(id) != ida_addr:
            if ida_override:
                if debug:
                    print(
                        f"Conflict Warning: ID {id} VR {id_vr.get(id)} with IDA {ida_addr}, using IDA"
                    )
                id_vr[id] = ida_addr
            else:
                if debug:
                    print(
                        f"Conflict Warning: ID {id} VR {id_vr.get(id)} with IDA {ida_addr}, ignoring IDA"
                    )
            conflicts += 1
            continue
        elif id not in id_vr and ida_addr:
            id_vr[id] = ida_addr
            id_vr_status[id] = {"status": CONFIDENCE["SUGGESTED"]}
    if debug:
        print(f"total ids {ids} conflicts {conflicts} percentage {conflicts/ids}")
    return loaded


async def scan_code(
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
    results = []
    defined_rel_ids = {}
    defined_vr_offsets = {}
    file_count = 0
    tasks = []
    for dirpath, dirnames, filenames in os.walk(a_directory):
        rem = []
        for dirname in dirnames:
            if dirname in a_exclude:
                rem.append(dirname)
        for todo in rem:
            dirnames.remove(todo)

        for filename in filenames:
            if filename not in a_exclude and filename.endswith(ALL_TYPES):
                file_count += 1
                tasks.append(
                    scan_file(
                        a_directory,
                        results,
                        defined_rel_ids,
                        defined_vr_offsets,
                        dirpath,
                        filename,
                    )
                )
    await asyncio.gather(*tasks)
    print(
        f"Finished scanning {file_count:n} files. rel_ids: {len(defined_rel_ids)} offsets: {len(defined_vr_offsets)} results: {len(results)}"
    )
    return {
        "defined_rel_ids": defined_rel_ids,
        "defined_vr_offsets": defined_vr_offsets,
        "results": results,
    }


async def scan_file(
    a_directory, results, defined_rel_ids, defined_vr_offsets, dirpath, filename
):
    await find_known_names(defined_rel_ids, defined_vr_offsets, dirpath, filename)
    if filename.lower().startswith("offset"):
        await parse_offsets(defined_rel_ids, defined_vr_offsets, dirpath, filename)
        # offset files historically (particularly in commonlib) were treated special because they were a source of truth for matched addresses
        # however, newer libraries (po3/ng) use macros that already have info
    await search_for_ids(
        a_directory,
        results,
        defined_rel_ids,
        defined_vr_offsets,
        dirpath,
        filename,
    )


async def search_for_ids(
    a_directory, results, defined_rel_ids, defined_vr_offsets, dirpath, filename
):
    """Search for SSE IDs that may need a VR counterpart defined.

    Args:
        a_directory (str): Root directory
        results (Dict[str]): Dict of defined_rel_ids, defined_vr_offsets, results
        defined_rel_ids (dict): dictionary of found rel_ids from offsets with key symbol name; primarily used when two different files may have sse and vr info to match.
        defined_vr_offsets (dict): dictionary of found vr offsets with key symbol name
        dirpath (str): directory path
        filename (str): filename
    """
    found_ifndef = False
    async with aiofiles.open(f"{dirpath}/{filename}", "r+") as f:
        try:
            data = mmap.mmap(f.fileno(), 0).read().decode("utf-8")
            f = await preProcessData(data)
            for i, line in enumerate(f.splitlines()):
                for regex_pattern in RELID_MATCH_ARRAY:
                    matches = [
                        m.groupdict()
                        for m in re.compile(
                            regex_pattern,
                            flags=re.IGNORECASE | re.MULTILINE,
                        ).finditer(line)
                    ]
                    if matches:
                        for match in matches:
                            if any(match):
                                if int(match.get("sse", 0)) < 1:
                                    # ids must be >= 0
                                    continue
                                if match.get("sse") and match.get("ae"):
                                    # update AE match database based on found items
                                    sse_id = int(match["sse"])
                                    ae_id = int(match.get("ae"))
                                    sse_ae[sse_id] = ae_id
                                if match.get("sse") and match.get("vr_idoffset"):
                                    # update VR match database based on found items
                                    vr = add_hex_strings(
                                        match.get("vr_idoffset"), SKYRIM_BASE
                                    )
                                    if sse_vr.get(sse_id) is None:
                                        sse_vr[sse_id] = vr
                                results.append(
                                    {
                                        "i": i,
                                        "directory": dirpath[len(a_directory) :],
                                        "filename": filename,
                                        "matches": match,
                                    }
                                )
                    if line.lower().startswith("#ifndef skyrimvr"):
                        found_ifndef = True
            if found_ifndef:
                f.seek(0)
                if debug:
                    print(
                        f"Searching for ifndef id offset definitions in {dirpath}/{filename}"
                    )
                ifndef_matches = re.findall(
                    IFNDEF_PATTERN,
                    f.read(),
                    flags=re.IGNORECASE | re.MULTILINE,
                )
                if ifndef_matches:
                    for match in ifndef_matches:
                        name = (
                            match[0] if not match[0].endswith("()") else match[0][:-2]
                        )
                        id = match[2]
                        offset = match[3]
                        func = {
                            "namespace": f"{filename}::",
                            "name": name,
                        }
                        defined_rel_ids[f"{filename}::{name}"] = {
                            "id": id,
                            "func": func,
                        }
                        defined_vr_offsets[f"{filename}::{name}"] = {
                            "id": offset,
                            "func": func,
                        }
                        if debug:
                            print(
                                f"Found ifndef {filename}::{name} with id: {id} offset: {offset}"
                            )
        except (UnicodeDecodeError, ValueError) as ex:
            print(f"Unable to read {dirpath}/{filename}: ", ex)


async def find_known_names(defined_rel_ids, defined_vr_offsets, dirpath, filename):
    global id_name
    global ae_name
    async with aiofiles.open(f"{dirpath}/{filename}", "r+") as f:
        try:
            data = mmap.mmap(f.fileno(), 0).read().decode("utf-8")
            for type_key, regex in FUNCTION_REGEX_PARSE_DICT.items():
                search = re.finditer(regex, await preProcessData(data), re.I)
                namespace = ""
                for m in search:
                    result = m.groupdict()
                    return_type = result.get("return_type", "")
                    funcName = result.get("func_name")
                    if result.get("class_decl") and namespace != result.get(
                        "class_decl"
                    ):
                        namespace = result.get("class_decl")
                    if result.get("class") and namespace != result.get("class"):
                        className = (
                            f"{namespace}::{result.get('class')}"
                            if namespace
                            else result.get("class")
                        )
                    elif result.get("class"):
                        className = result.get("class")
                    else:
                        className = ""
                    fullName = f"{className}::{funcName}" if className else funcName
                    args = " ".join(result.get("args", "").split())
                    if result.get("id"):
                        id = int(result.get("id"))
                    elif result.get("sseid"):
                        id = int(result.get("sseid"))
                    name_string = ""
                    if return_type:
                        name_string += f"{return_type} "
                    if fullName:
                        name_string += f"{fullName}"
                    if args:
                        name_string += f"({args})"
                    if id:
                        id_name[id] = name_string
                        if debug:
                            print(f"Found ID {id}: {id_name[id]}")
                    if result.get("aeid"):
                        aeid = int(result.get("aeid"))
                        ae_name[aeid] = name_string
                        if debug:
                            print(f"Found AE_ID {aeid}: {ae_name[aeid]}")
        except (UnicodeDecodeError, ValueError) as ex:
            print(f"Unable to read {dirpath}/{filename}: ", ex)


async def parse_offsets(defined_rel_ids, defined_vr_offsets, dirpath, filename):
    """Parse offset files to define items

    Args:
        defined_rel_ids (dict): dictionary of found rel_ids from offsets with key symbol name
        defined_vr_offsets (dict): dictionary of found vr offsets with key symbol name
        dirpath (str): directory path
        filename (str): filename
    """
    # looking at offsets
    if debug:
        print("parsing offsets file: ", f"{dirpath}/{filename}")
        # if filename.lower() == map(lambda x: x.lower(), ["Offsets_VTABLE.h"]):
        #     regex_parse(defined_rel_ids, dirpath, filename)
        # else:
    await cpp_header_parse(defined_rel_ids, defined_vr_offsets, dirpath, filename)
    await regex_parse(defined_rel_ids, defined_vr_offsets, dirpath, filename)


async def regex_parse(defined_rel_ids, defined_vr_offsets, dirpath, filename):
    async with aiofiles.open(f"{dirpath}/{filename}", "r+") as f:
        try:
            data = mmap.mmap(f.fileno(), 0).read().decode("utf-8")
            f = await preProcessData(data)
            for i, line in enumerate(f.splitlines()):
                for type_key, regex in REGEX_PARSE_DICT.items():
                    namespace = "RE::"
                    search = re.finditer(regex, line, re.I)
                    for item_count, item in enumerate(search):
                        name = ""
                        if item.group() and item.group(1):
                            name = item.group(1)
                        try:
                            id = item.group(5)
                        except IndexError:
                            try:
                                id = item.group(3)
                            except IndexError:
                                id = item.group(2)
                        if "vtable" in type_key and name:
                            full_name = f"{name}_{item_count}"
                        else:
                            full_name = name
                        if debug:
                            print("Found", type_key, full_name, id)
                        if type_key in [REL_ID_VTABLE, REL_ID]:
                            defined_rel_ids[f"{namespace}{full_name}"] = {
                                "id": str(id),
                                "name": full_name,
                            }
                            id_name[id] = full_name
                        elif type_key in [REL_OFFSET_VTABLE, REL_OFFSET]:
                            defined_vr_offsets[f"{namespace}{full_name}"] = {
                                "id": str(id),
                                "name": full_name,
                            }

        except UnicodeDecodeError as ex:
            print(f"Unable to read {dirpath}/{filename}: ", ex)


async def cpp_header_parse(
    defined_rel_ids, defined_vr_offsets, dirpath, filename
) -> bool:
    result = False
    try:
        async with aiofiles.open(f"{dirpath}/{filename}", "r+") as f:
            data = mmap.mmap(f.fileno(), 0).read().decode("utf-8")
            processed_data = await preProcessData(data)
            # Next line solves bug https://github.com/robotpy/robotpy-cppheaderparser/issues/83
            processed_data = re.sub("\n#line .*", "\n", processed_data)
            header = CppHeaderParser.CppHeader(
                processed_data,
                argType="string",
                preprocessed=True,
            )
    except CppHeaderParser.CppHeaderParser.CppParseError as ex:
        print(f"Unable to cppheaderparse {dirpath}/{filename}: ", ex)
        return result
    for func in header.functions:
        if func.get("returns") == "constexpr REL::ID":
            result = True
            name = func.get("name")
            namespace = func.get("namespace")
            search = re.search(OFFSET_RELID_PATTERN, func.get("debug"), re.I)
            if search and search.groups():
                id = search.groups()[1]
                if int(id) < 1:
                    continue
                if debug:
                    print("Found rel::id", name, id)
                defined_rel_ids[f"{namespace}{name}"] = {
                    "id": id,
                    "func": func,
                }
        elif func.get("returns") == "constexpr REL::Offset":
            result = True
            name = func.get("name")
            namespace = func.get("namespace")
            search = re.search(OFFSET_OFFSET_PATTERN, func.get("debug"), re.I)
            if search and search.groups():
                id = search.groups()[1]
                if debug:
                    print("Found rel::offset", name, id)
                defined_vr_offsets[f"{namespace}{name}"] = {
                    "id": id,
                    "func": func,
                }
    return result


def analyze_code_offsets(defined_rel_ids: dict, defined_vr_offsets: dict):
    """Analyze rel::id and rel::offsets defined in code to mark id_vr items as verified.

    Args:
        defined_rel_ids (dict): rel::ids defined in code. The key is a cpp name designation and the value is the ID.
        defined_vr_offsets (dict): rel::offsets defined in code. The key is a name designation and we assume the same namespace defined as an offset is a VR offset.
    """
    global id_vr_status
    global debug
    if defined_rel_ids and defined_vr_offsets:
        if debug:
            print(
                f"Identifying known offsets from code: {len(defined_rel_ids)} offsets: {len(defined_vr_offsets)}"
            )
        verified = 0
        unverified = 0
        mismatch = 0
        missing = 0
        ida_suggested = 0
        for k, v in defined_vr_offsets.items():
            # Iterate over all discovered vr offsets.
            try:
                if defined_rel_ids.get(k):
                    id = int(defined_rel_ids[k].get("id"))
                    defined_rel_ids[k]["sse"] = sse_addr = add_hex_strings(id_sse[id])
                    bakou_vr_addr = add_hex_strings(id_vr[id]) if id_vr.get(id) else 0
                    code_vr_addr = add_hex_strings(v.get("id"), SKYRIM_BASE)
                    if (
                        sse_vr.get(sse_addr)
                        and bakou_vr_addr
                        and sse_vr[sse_addr] != bakou_vr_addr
                    ):
                        if debug:
                            print(
                                f"WARNING: {k} IDA {sse_vr[sse_addr]} and bakou {bakou_vr_addr} conversions do not match",
                            )
                    if bakou_vr_addr and code_vr_addr == bakou_vr_addr:
                        defined_rel_ids[k]["status"] = (
                            CONFIDENCE["VERIFIED"]
                            if id_vr_status.get(id, {}).get("status") is None
                            else id_vr_status.get(id, {}).get("status")
                        )
                        if debug:
                            print(f"MATCHED: {k} ID: {id} matches database")
                        id_vr_status[id] = defined_rel_ids[k]
                        verified += 1
                    elif not bakou_vr_addr and code_vr_addr:
                        if debug:
                            print(
                                f"Using defined offset address: {id} {k} defined: {code_vr_addr}",
                            )
                        defined_rel_ids[k]["status"] = (
                            CONFIDENCE["MANUAL"]
                            if id_vr_status.get(id, {}).get("status") is None
                            else id_vr_status.get(id, {}).get("status")
                        )
                        id_vr_status[id] = defined_rel_ids[k]
                        id_vr_status[id]["vr"] = code_vr_addr
                        verified += 1
                    else:
                        if debug:
                            print(
                                f"Potential mismatch with databases: {id} {k} defined: {code_vr_addr} Databases: {bakou_vr_addr} id_sse {sse_addr} sse_vr {sse_vr.get('sse_addr')}",
                            )
                        defined_rel_ids[k]["status"] = (
                            CONFIDENCE["MANUAL"]
                            if id_vr_status.get(id, {}).get("status") is None
                            else id_vr_status.get(id, {}).get("status")
                        )
                        id_vr_status[id] = defined_rel_ids[k]
                        mismatch += 1
            except KeyError:
                id = int(defined_rel_ids[k].get("id"))
                if debug:
                    print(
                        f"Unable to verify: {k} ID: {id} not in databases. id_sse: {id_sse[id]} sse_vr: {sse_vr.get(add_hex_strings(id_sse[id]))}"
                    )
                unverified += 1
        # use databases to suggest addresses for rel::id items
        for k, v in defined_rel_ids.items():
            id = int(v.get("id"))
            if id in id_vr_status:
                continue
            if id in id_sse:
                sse_addr = add_hex_strings(id_sse[id])
                v["sse"] = sse_addr
                if (
                    sse_addr in sse_vr
                    and v.get("status", CONFIDENCE["UNKNOWN"]) < CONFIDENCE["MANUAL"]
                ):
                    if debug:
                        print(
                            f"Found suggested address: {sse_vr[sse_addr]} for {k} with IDA. SSE: {sse_addr} "
                        )
                    if v.get("status", CONFIDENCE["UNKNOWN"]) < CONFIDENCE["SUGGESTED"]:
                        v["status"] = CONFIDENCE["SUGGESTED"]
                    elif v.get("status") is None:
                        v["status"] = CONFIDENCE["UNKNOWN"]
                    id_vr_status[id] = v
                    ida_suggested += 1
            if v.get("status", CONFIDENCE["UNKNOWN"]) < CONFIDENCE["MANUAL"]:
                if debug:
                    print(f"Missing VR offset {v.get('id')} for {k} ")
                v["status"] = v.get("status", CONFIDENCE["UNKNOWN"])
                id_vr_status[id] = v
                missing += 1
        print(
            f"Database matched: {verified} ida_suggested: {ida_suggested} unverified: {unverified} mismatch: {mismatch} missing: {missing}"
        )


def match_results(
    results: List[dict], min_confidence=CONFIDENCE["SUGGESTED"], database=False
) -> List[dict]:
    """Match result ids to known vr addresses that meet min_confidence.

    Args:
        results (List[dict]): A list of results from scan_code
        min_confidence (int, optional): Minimum confidence level to match. Defaults to SUGGESTED == 1
        database (bool, optional): Whether to output in a database.csv format for manual editing. Defaults to False

    Returns:
        List[dict]: Sorted list of results. Default is a tab-separated file for linting.
    """
    global id_vr_status
    new_results = []
    for result in results:
        i = result["i"]
        directory = result["directory"]
        filename = result["filename"]
        match = result["matches"]
        offset: int = 0
        conversion = ""
        vr_addr = ""
        warning = ""
        updateDatabase = False
        suggested_vr = ""
        if match.get("id_with_offset"):
            id = int(match.get("id_with_offset"))
            offset = match.get("offset", 0)
        elif match.get("sse"):
            id = int(match.get("sse"))
            try:
                offset = (
                    int(match.get("sse_offset", 0)) if match.get("sse_offset") else 0
                )
            except ValueError:  # it's a hex string e.g., 0x2e6
                offset = (
                    int(match.get("sse_offset", 0), 16)
                    if match.get("sse_offset")
                    else 0
                )
        elif match.get("id"):
            id = int(match.get("id"))
            offset = 0
        else:
            continue
        if (
            id_vr.get(id) is None
            or int(id_vr_status.get(id, {}).get("status", 0)) == CONFIDENCE["SUGGESTED"]
        ):
            updateDatabase = True
        status = int(id_vr_status.get(id, {}).get("status", 0))
        if id_vr.get(id) and status >= min_confidence:
            vr_addr = id_vr[id]
            conversion = f"REL::Offset(0x{vr_addr[4:]})"
            suggested_vr = vr_addr
            if (
                offset
                and int(id_vr_status.get(id, {}).get("status", 0))
                < CONFIDENCE["PERFECT"]
            ):
                warning = f"WARNING: Offset detected; offset may need to be manually updated for VR"
        elif id_vr_status.get(id, {}).get("vr") and status >= min_confidence:
            suggested_vr = id_vr_status[id]["vr"]
        if not vr_addr:
            warning += f"WARNING: VR Address undefined."
        try:
            sse_addr = id_sse[id]
        except KeyError:
            conversion = "UNKNOWN"
            sse_addr = ""
        if offset and not conversion:
            conversion = f"UNKNOWN SSE_{sse_addr}{f'+{offset}={add_hex_strings(sse_addr, offset)}' if offset else ''}"
        if database and updateDatabase:
            status = 1 if status == 0 else status
            if ae_name.get(sse_ae.get(id)):
                description = ae_name.get(sse_ae.get(id))
            else:
                description = f"{directory[1:] if directory.startswith('/') or directory.startswith(chr(92)) else directory}/{filename}:{i+1}"
            if id_name.get(id):
                description = f"{id_name.get(id)} {description}"
            elif match.get("name"):
                description = f"{match.get('name')} {description}"
            new_results.append(f"{id},{sse_addr},{suggested_vr},{status},{description}")
        elif not database:
            new_results.append(
                f"{directory}/{filename}:{i+1}\tID: {id}\tFLAT: {sse_addr}\t{conversion}\t{vr_addr}\t{warning}"
            )
    if database:
        return sorted(new_results, key=lambda line: int(line.split(",")[0]))
    return sorted(new_results)


def in_file_replace(results: List[str]) -> bool:
    """Replace instances of REL::ID with an #ifndef SKYRIMVR.

    Args:
        results (List[str]): [description]

    Returns:
        bool: Whether successful
    """
    for line in results:
        parts = line.split("\t")
        print(parts)
        filename, line_number = parts[0].split(":")
        text_to_replace = parts[1]
        sse_addr = parts[2]
        vr_addr = parts[4]
        if parts[3].startswith("UNKNOWN"):
            replacement = f"REL::Offset({parts[3]})"
        else:
            replacement = parts[3]
        with fileinput.FileInput(filename, inplace=True) as file:
            print(f"Performing replace for {parts[0]}")
            found_ifndef = False
            for i, line in enumerate(file):
                if "#ifndef SKYRIMVR".lower() in line.lower():
                    found_ifndef = True
                    print(line, end="")
                elif found_ifndef and text_to_replace in line:
                    found_ifndef = False
                    print(line, end="")
                else:
                    print(
                        line.replace(
                            text_to_replace,
                            REPLACEMENT.format(
                                text_to_replace,
                                sse_addr,
                                replacement,
                                vr_addr,
                            ),
                        ),
                        end="",
                    )
    return True


async def write_csv(
    file_prefix: str = "version",
    version: str = "1-4-15-0",
    min_confidence=CONFIDENCE["MANUAL"],
    generate_database=False,
    release_version="0.0.0",
    skyrim=True,
) -> bool:
    """Generate csv file.

    Args:
        file_prefix (str, optional): Filename prefix to output. Defaults to "version".
        version (str, optional): Version suffix. Defaults to "1-4-15-0".
        min_confidence (int, optional): Minimum confidence to output. Defaults to CONFIDENCE["MANUAL"] == 2.
        generate_database (bool, optional): Whether to generate a database file (used for GitHub editing) instead of an Skyrim importable address.csv. Defaults to False.
        release_version (str, optional): CSV version. Defaults to "0.0.0".
    Returns:
        bool: Whether successful.
    """
    global id_vr_status
    global id_name
    version = version if skyrim and version == "1-4-15-0" else "1-2-72-0"
    outputfile = (
        f"{file_prefix}-{version}.csv"
        if not generate_database
        else f"database.csv" if skyrim else "fo4_database.csv"
    )
    output = {}
    if min_confidence is not None and isinstance(min_confidence, int):
        output = dict(
            filter(
                lambda elem: (
                    id_vr_status.get(elem[0])
                    and id_vr_status.get(elem[0]).get("status", 0)
                    and int(id_vr_status.get(elem[0]).get("status", 0))
                    >= min_confidence
                )
                or (
                    min_confidence == CONFIDENCE["UNKNOWN"]
                    and elem[0] not in id_vr_status
                ),
                id_vr.items(),
            )
        )
        print(
            f"Filtered {len(id_vr)} to {len(output)} using min_confidence {min_confidence}"
        )
    else:
        output = id_vr
    try:
        async with aiofiles.open(outputfile, "w", newline="") as f:
            writer = aiocsv.AsyncWriter(f)
            rows = len(output)
            if not generate_database:
                await writer.writerow(("id", "offset"))
                await writer.writerow((rows, release_version))
                for id, address in sorted(output.items()):
                    if address[4:]:
                        await writer.writerow((id, address[4:]))
            else:
                await writer.writerow(
                    ("id", "sse" if skyrim else "fo4", "vr", "status", "name")
                )
                for key, value in id_vr_status.items():
                    # add defined offsets items
                    if "vr" in value:
                        output[key] = value["vr"].lower()
                rows = len(output)
                for id, address in sorted(output.items()):
                    sse_addr = ""
                    status = ""
                    name = ""
                    if id_vr_status.get(id):
                        entry = id_vr_status.get(id)
                        sse_addr = entry.get("sse", "")
                        status = entry["status"]
                        # use entry name unless we have an id_name mapping
                        name = (
                            entry.get("name")
                            if not id_name.get(id)
                            else id_name.get(id)
                        )
                        # check pdbs
                        pdb_name = offset_name.get(sse_addr)
                        if pdb_name:
                            if name and name != pdb_name:
                                print(f"{id}: Replacing {name} with {pdb_name}")
                            name = pdb_name
                        # add cpp parser names from offsets file
                        if not name and entry.get("func"):
                            name = (
                                f'{entry["func"]["namespace"]}{entry["func"]["name"]}'
                            )
                        # only add unknown names from ae_name
                        if not name and sse_ae.get(id) and ae_name.get(sse_ae.get(id)):
                            name = ae_name.get(sse_ae.get(id))
                    await writer.writerow((id, sse_addr, address, status, name))
            print(
                f"Wrote {rows} rows into {outputfile} with release version {release_version}"
            )
            return True
    except OSError as ex:
        print(f"Error writing to {outputfile}: {ex}")
        return False


async def write_ae_map() -> bool:
    """Generate sse ae csv file.
    Returns:
        bool: Whether successful.
    """
    global id_vr_status
    global ae_name
    global sse_ae
    outputfile = "se_ae.csv"
    output = {}
    try:
        async with aiofiles.open(outputfile, "w", newline="") as f:
            writer = aiocsv.AsyncWriter(f)
            rows = len(sse_ae)
            await writer.writerow(("sseid", "aeid", "confidence", "name"))
            for id, ae in sorted(sse_ae.items()):
                name = ""
                confidence = CONFIDENCE["SUGGESTED"]
                if id_vr_status.get(id):
                    entry = id_vr_status.get(id)
                    name = entry.get("name")
                    confidence = CONFIDENCE["VERIFIED"]
                    # add cpp parser names from offsets file
                    if not name and entry.get("func"):
                        name = f'{entry["func"]["namespace"]}{entry["func"]["name"]}'
                    # only add unknown names from ae_name
                if not name and sse_ae.get(id) and ae_name.get(sse_ae.get(id)):
                    name = ae_name.get(sse_ae.get(id))
                await writer.writerow((id, ae, confidence, name))
        print(f"Wrote {rows} rows into {outputfile}")
        return True
    except OSError as ex:
        print(f"Error writing to {outputfile}: {ex}")
        return False


async def main():
    global debug
    global args
    global cpp
    parser = argparse.ArgumentParser(
        description="Find uses of REL::ID in cpp files. By default, performs a lint to display a list of files besides Offsets*.h which are using REL::ID and should be converted for VR. Unknown addresses will be prefaced SSE_."
    )

    def dir_path(path):
        if os.path.isdir(path):
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
    parser.add_argument(
        "-f",
        "--fallout",
        action="store_true",
        help="Analyze Fallout4 instead of Skyrim.",
    )
    subparsers = parser.add_subparsers(dest="subparser")

    parser_analyze = subparsers.add_parser(
        "analyze",
        help="Analyze code to determine manually identified ids and vr offsets. Will also check against bin-diffed address databases.",
    )
    parser_analyze.add_argument(
        "-m",
        "--min",
        nargs="?",
        const="minimum",
        action="store",
        help="Sets the minimum confidence needed for an ID match. Defaults to 2.",
    )
    parser_analyze.add_argument(
        "-d",
        "--database",
        action="store_true",
        help="Output failed ID matches in database.csv format; used for manual editing.",
    )
    parser_replace = subparsers.add_parser(
        "replace",
        help="Replace files automatically inline with bin-diffed discovered addresses within an #ifndef SKYRIMVR. Unknown addresses will be prefaced SSE_ and need to be manually fixed. This should be used for quick address testing only because it is preferred to make fixes in a new VR csv.",
    )
    parser_generate = subparsers.add_parser(
        "generate",
        help="Generate a version-1.4-15.0.csv with offsets.",
    )
    parser_generate.add_argument(
        "--prefix",
        nargs="?",
        const="version",
        action="store",
        help="Sets the prefix for the csv. Defaults to version.",
    )
    parser_generate.add_argument(
        "--min",
        nargs="?",
        const="minimum",
        action="store",
        help="Sets the minimum confidence for the csv. Defaults to 2.",
    )
    parser_generate.add_argument(
        "-d",
        "--database",
        action="store_true",
        help="Generate database.csv.",
    )
    parser_generate.add_argument(
        "-m",
        "--map",
        action="store_true",
        help="Generate se_ae.csv mapping of se to ae addresses.",
    )
    parser_generate.add_argument(
        "-rv",
        "--release_version",
        nargs="?",
        const="release_version",
        action="store",
        help="Sets the release version. Defaults to 0.0.0.",
    )

    args = vars(parser.parse_args())
    debug = args.get("debug")
    fallout = args.get("fallout", False)
    if debug:
        print(args)
    exclude = ["build", "buildvr", "extern", "external"]
    scan_results = {}
    cpp = preParser()  # init preprocessor
    # Load files from location of python script
    if (
        await load_database(
            ida_override=True,
            skyrim=not fallout,
            offsets="offsets-1.5.97.0.csv" if not fallout else "offsets-1-10-163-0.csv",
            ida_compare="sse_vr.csv" if not fallout else "fo4_vr.csv",
        )
        == 0
    ):
        print("Error, no databases loaded. Exiting.")
        exit(1)
    else:
        if args["path"]:
            root = args["path"]
            os.chdir(root)
        else:
            root = os.path.split(os.path.dirname(os.path.realpath(__file__)))[0]
            os.chdir(root)
        scan_results = await scan_code(
            root,
            exclude,
        )
    analyze = args.get("subparser") == "analyze"
    replace = args.get("subparser") == "replace"
    generate = (
        args.get("prefix")
        if args.get("subparser") == "generate" and args.get("prefix")
        else args.get("subparser") == "generate"
    )
    defined_rel_ids = scan_results["defined_rel_ids"]
    defined_vr_offsets = scan_results["defined_vr_offsets"]
    minimum = 2
    if args.get("min") is not None:
        minimum = int(args.get("min"))
    analyze_code_offsets(defined_rel_ids, defined_vr_offsets)
    if generate:
        sub_args = {"min_confidence": minimum}
        if fallout:
            sub_args["skyrim"] = False
        if args.get("database"):
            sub_args["generate_database"] = True
        if generate and not isinstance(generate, bool):
            sub_args["file_prefix"] = generate
        if args.get("release_version"):
            sub_args["release_version"] = args.get("release_version")
        if args.get("map"):
            await write_ae_map()
        else:
            await write_csv(**sub_args)
    elif analyze and scan_results.get("results"):
        results = match_results(
            scan_results["results"],
            min_confidence=minimum,
            database=args.get("database"),
        )
        if replace:
            in_file_replace(results)
        else:
            print(*results, sep="\n")
            print(f"Found {len(results):n} items")
            exit(len(results))


if __name__ == "__main__":
    asyncio.run(main())
