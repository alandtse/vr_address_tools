import re
import argparse
import os
from typing import Dict, Tuple, Union

# Define a type alias for offsets
OffsetsType = Dict[str, Union[int, Tuple[int, Union[int, Tuple]]]]


def extract_offsets(offset_file_path: str) -> OffsetsType:
    """
    Extract offsets from the given offset file.

    Args:
        offset_file_path (str): Path to the offset.h file.

    Returns:
        Dict[str, Union[int, Tuple[int, Union[int, Tuple]]]]: Dictionary of offsets.
    """
    offsets: OffsetsType = {}
    namespace_stack = []

    with open(offset_file_path, "r") as file:
        lines = file.readlines()

    for line in lines:
        namespace_match = re.match(r"\s*namespace (\w+)", line)
        if namespace_match:
            namespace_stack.append(namespace_match.group(1))
        elif re.match(r"\s*}\s*", line):
            if namespace_stack:
                namespace_stack.pop()
        else:
            offset_match = re.match(
                r".*REL::ID (\w+)\(static_cast<std::uint64_t>\((\d+)\)\);", line
            )
            if offset_match and namespace_stack:
                func_name = offset_match.group(1)
                func_id = int(offset_match.group(2))
                key = "::".join(namespace_stack) + f"::{func_name}"
                if key in offsets:
                    offsets[key] = (func_id, offsets[key])
                else:
                    offsets[key] = func_id

    return offsets


def replace_offsets_in_file(
    file_path: str, offsets: OffsetsType, name_space: str = ""
) -> Tuple[int, int]:
    """
    Replace offsets in the specified C++ file.

    Args:
        file_path (str): Path to the C++ file.
        offsets (Dict[str, Union[int, Tuple[int, Union[int, Tuple]]]]): Dictionary of offsets.

    Returns:
        Tuple[int, int]: Number of offsets replaced and number of warnings.
    """
    warnings = 0
    with open(file_path, "r") as file:
        content = file.read()

    # Regular expression to find the offset references
    pattern = re.compile(r"Offset::([\w:]+)(?:::(\w+))*")

    # Replace each match with the corresponding REL::ID
    def replace_match(match):
        nonlocal warnings
        if match.group(2):
            full_key = f"{name_space}{match.group(1)}::{match.group(2)}"
        full_key = f"{name_space}{match.group(1)}"    
        if full_key in offsets:
            if isinstance(offsets[full_key], tuple):
                return f"RELOCATION_ID{offsets[full_key]}"
            return f"REL::ID({offsets[full_key]})"
        warnings += 1
        print(f"Warning: {full_key} not found")
        return match.group(0)

    updated_content = re.sub(pattern, replace_match, content)

    with open(file_path, "w") as file:
        file.write(updated_content)

    if updated_content != content:
        print(f"Offsets replaced in {file_path}")
        return 1, warnings
    else:
        print(f"Processed {file_path}")
        return 0, warnings


def replace_offsets_in_directory(
    directory_path: str, offsets: OffsetsType, name_space: str = ""
) -> Tuple[int, int, int]:
    """
    Traverse directory and process all C++ files to replace offsets.

    Args:
        directory_path (str): Path to the directory.
        offsets (Dict[str, Union[int, Tuple[int, Union[int, Tuple]]]]): Dictionary of offsets.

    Returns:
        Tuple[int, int, int]: Number of files processed, offsets replaced, and total warnings.
    """
    files_processed = 0
    offsets_replaced = 0
    total_warnings = 0

    for root, _, files in os.walk(directory_path):
        for file in files:
            if file.endswith(".cpp") or file.endswith(".h"):
                file_path = os.path.join(root, file)
                replaced, warnings = replace_offsets_in_file(
                    file_path, offsets, name_space
                )
                files_processed += 1
                offsets_replaced += replaced
                total_warnings += warnings

    return files_processed, offsets_replaced, total_warnings


def main():
    """
    Main function to parse arguments and replace offset references in C++ files.
    """
    parser = argparse.ArgumentParser(
        description="Replace offset references in C++ files with their corresponding REL::ID values."
    )
    parser.add_argument("offset_file", help="Path to the offset.h file")
    parser.add_argument(
        "cpp_path",
        const=".",
        default=".",
        help="Path to the C++ file or directory to be processed",
        nargs="?",
        type=str,
    )
    parser.add_argument(
        "default_namespace",
        const="RE::Offset::",
        default="RE::Offset::",
        help="Default namespace to assume",
        nargs="?",
        type=str,
    )
    args = parser.parse_args()

    # Extract offsets from offset.h
    offsets = extract_offsets(args.offset_file)

    # Check if cpp_path is a file or directory
    if os.path.isdir(args.cpp_path):
        files_processed, offsets_replaced, total_warnings = (
            replace_offsets_in_directory(args.cpp_path, offsets, args.default_namespace)
        )
    elif os.path.isfile(args.cpp_path):
        files_processed = 1
        offsets_replaced, total_warnings = replace_offsets_in_file(
            args.cpp_path, offsets, args.default_namespace
        )
    else:
        print(f"Error: {args.cpp_path} is not a valid file or directory")
        return

    print(f"Files reviewed: {files_processed}")
    print(f"Offsets replaced: {offsets_replaced}")
    print(f"Total warnings: {total_warnings}")


if __name__ == "__main__":
    main()
