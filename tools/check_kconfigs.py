#!/usr/bin/env python
#
# Copyright 2018 Espressif Systems (Shanghai) PTE LTD
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import print_function
from __future__ import unicode_literals
import os
import sys
import re
import argparse
from io import open

# regular expression search object for matching Kconfig files
RE_KCONFIG = re.compile(r'^Kconfig(?:\.projbuild)?$')

# ouput file with suggestions will get this suffix
OUTPUT_SUFFIX = '.new'

# ignored directories (makes sense only when run on IDF_PATH)
# Note: IGNORE_DIRS is a tuple in order to be able to use it directly with the startswith() built-in function which
# accepts tuples but no lists.
IGNORE_DIRS = (
    # Kconfigs from submodules need to be ignored:
    os.path.join('components', 'mqtt', 'esp-mqtt'),
)

SPACES_PER_INDENT = 4

# TODO decrease the value (after the names have been refactored)
CONFIG_NAME_MAX_LENGTH = 60

# TODO increase prefix length (after the names have been refactored)
CONFIG_NAME_MIN_PREFIX_LENGTH = 0

# list or rules for lines
LINE_ERROR_RULES = [
    # (regular expression for finding,      error message,                                  correction)
    (re.compile(r'\t'),                     'tabulators should be replaced by spaces',      r' ' * SPACES_PER_INDENT),
    (re.compile(r'\s+\n'),                  'trailing whitespaces should be removed',       r'\n'),
    (re.compile(r'.{120}'),                 'line should be shorter than 120 characters',   None),
]


class InputError(RuntimeError):
    """
    Represents and error on the input
    """
    def __init__(self, path, line_number, error_msg, suggested_line):
        super(InputError, self).__init__('{}:{}: {}'.format(path, line_number, error_msg))
        self.suggested_line = suggested_line


class BaseChecker(object):
    """
    Base class for all checker objects
    """
    def __init__(self, path_in_idf):
        self.path_in_idf = path_in_idf

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        pass


class LineRuleChecker(BaseChecker):
    """
    checks LINE_ERROR_RULES for each line
    """
    def process_line(self, line, line_number):
        errors = []
        for rule in LINE_ERROR_RULES:
            m = rule[0].search(line)
            if m:
                errors.append(rule[1])
                if rule[2]:
                    line = rule[0].sub(rule[2], line)
        if len(errors) > 0:
            raise InputError(self.path_in_idf, line_number, "; ".join(errors), line)


class IndentAndNameChecker(BaseChecker):
    """
    checks the indentation of each line and configuration names
    """
    def __init__(self, path_in_idf, debug=False):
        super(IndentAndNameChecker, self).__init__(path_in_idf)
        self.debug = debug
        self.min_prefix_length = CONFIG_NAME_MIN_PREFIX_LENGTH

        # stack of the nested menuconfig items, e.g. ['mainmenu', 'menu', 'config']
        self.level_stack = []

        # stack common prefixes of configs
        self.prefix_stack = []

        # if the line ends with '\' then we force the indent of the next line
        self.force_next_indent = 0

        # menu items which increase the indentation of the next line
        self.re_increase_level = re.compile(r'''^\s*
                                          (
                                               (menu(?!config))
                                              |(mainmenu)
                                              |(choice)
                                              |(config)
                                              |(menuconfig)
                                              |(help)
                                              |(if)
                                          )
                                       ''', re.X)

        # closing menu items which decrease the indentation
        self.re_decrease_level = re.compile(r'''^\s*
                                          (
                                               (endmenu)
                                              |(endchoice)
                                              |(endif)
                                          )
                                       ''', re.X)

        # matching beginning of the closing menuitems
        self.pair_dic = {'endmenu': 'menu',
                         'endchoice': 'choice',
                         'endif': 'if',
                         }

        # regex for config names
        self.re_name = re.compile(r'''^
                                       (
                                            (?:config)
                                           |(?:menuconfig)
                                           |(?:choice)

                                       )\s+
                                       (\w+)
                                      ''', re.X)

        # regex for new prefix stack
        self.re_new_stack = re.compile(r'''^
                                            (
                                                 (?:menu(?!config))
                                                |(?:mainmenu)
                                                |(?:choice)

                                            )
                                            ''', re.X)

    def __exit__(self, type, value, traceback):
        super(IndentAndNameChecker, self).__exit__(type, value, traceback)
        if len(self.prefix_stack) > 0:
            self.check_common_prefix('', 'EOF')
        if len(self.prefix_stack) != 0:
            if self.debug:
                print(self.prefix_stack)
            raise RuntimeError("Prefix stack should be empty. Perhaps a menu/choice hasn't been closed")

    def del_from_level_stack(self, count):
        """ delete count items from the end of the level_stack """
        if count > 0:
            # del self.level_stack[-0:] would delete everything and we expect not to delete anything for count=0
            del self.level_stack[-count:]

    def update_level_for_inc_pattern(self, new_item):
        if self.debug:
            print('level+', new_item, ': ', self.level_stack, end=' -> ')
        # "config" and "menuconfig" don't have a closing pair. So if new_item is an item which need to be indented
        # outside the last "config" or "menuconfig" then we need to find to a parent where it belongs
        if new_item in ['config', 'menuconfig', 'menu', 'choice', 'if']:
            # item is not belonging to a previous "config" or "menuconfig" so need to indent to parent
            for i, item in enumerate(reversed(self.level_stack)):
                if item in ['menu', 'mainmenu', 'choice', 'if']:
                    # delete items ("config", "menuconfig", "help") until the appropriate parent
                    self.del_from_level_stack(i)
                    break
            else:
                # delete everything when configs are at top level without a parent menu, mainmenu...
                self.del_from_level_stack(len(self.level_stack))

        self.level_stack.append(new_item)
        if self.debug:
            print(self.level_stack)
        # The new indent is for the next line. Use the old one for the current line:
        return len(self.level_stack) - 1

    def update_level_for_dec_pattern(self, new_item):
        if self.debug:
            print('level-', new_item, ': ', self.level_stack, end=' -> ')
        target = self.pair_dic[new_item]
        for i, item in enumerate(reversed(self.level_stack)):
            # find the matching beginning for the closing item in reverse-order search
            # Note: "menuconfig", "config" and "help" don't have closing pairs and they are also on the stack. Now they
            # will be deleted together with the "menu" or "choice" we are closing.
            if item == target:
                i += 1  # delete also the matching beginning
                if self.debug:
                    print('delete ', i, end=' -> ')
                self.del_from_level_stack(i)
                break
        if self.debug:
            print(self.level_stack)
        return len(self.level_stack)

    def check_name_and_update_prefix(self, line, line_number):
        m = self.re_name.search(line)
        if m:
            name = m.group(2)
            name_length = len(name)

            if name_length > CONFIG_NAME_MAX_LENGTH:
                raise InputError(self.path_in_idf, line_number,
                                 '{} is {} characters long and it should be {} at most'
                                 ''.format(name, name_length, CONFIG_NAME_MAX_LENGTH),
                                 line + '\n')  # no suggested correction for this
            if len(self.prefix_stack) == 0:
                self.prefix_stack.append(name)
            elif self.prefix_stack[-1] is None:
                self.prefix_stack[-1] = name
            else:
                # this has nothing common with paths but the algorithm can be used for this also
                self.prefix_stack[-1] = os.path.commonprefix([self.prefix_stack[-1], name])
            if self.debug:
                print('prefix+', self.prefix_stack)
        m = self.re_new_stack.search(line)
        if m:
            self.prefix_stack.append(None)
            if self.debug:
                print('prefix+', self.prefix_stack)

    def check_common_prefix(self, line, line_number):
        common_prefix = self.prefix_stack.pop()
        if self.debug:
            print('prefix-', self.prefix_stack)
        if common_prefix is None:
            return
        common_prefix_len = len(common_prefix)
        if common_prefix_len < self.min_prefix_length:
            raise InputError(self.path_in_idf, line_number,
                             'Common prefix "{}" length is {} and should be at least {} characters long'
                             ''.format(common_prefix, common_prefix_len, self.min_prefix_length),
                             line)   # no suggested correction for this
        if len(self.prefix_stack) > 0:
            parent_prefix = self.prefix_stack[-1]
            if parent_prefix is None:
                # propagate to parent level where it will influence the prefix checking with the rest which might
                # follow later on that level
                self.prefix_stack[-1] = common_prefix
            else:
                if len(self.level_stack) > 0 and self.level_stack[-1] in ['mainmenu', 'menu']:
                    # the prefix from menu is not required to propagate to the children
                    return
                if not common_prefix.startswith(parent_prefix):
                    raise InputError(self.path_in_idf, line_number,
                                     'Common prefix "{}" should start with {}'
                                     ''.format(common_prefix, parent_prefix),
                                     line)   # no suggested correction for this

    def process_line(self, line, line_number):
        stripped_line = line.strip()
        if len(stripped_line) == 0:
            self.force_next_indent = 0
            return
        current_level = len(self.level_stack)
        m = re.search(r'\S', line)  # indent found as the first non-space character
        if m:
            current_indent = m.start()
        else:
            current_indent = 0

        if current_level > 0 and self.level_stack[-1] == 'help':
            if current_indent >= current_level * SPACES_PER_INDENT:
                # this line belongs to 'help'
                self.force_next_indent = 0
                return

        if self.force_next_indent > 0:
            if current_indent != self.force_next_indent:
                raise InputError(self.path_in_idf, line_number,
                                 'Indentation consists of {} spaces instead of {}'.format(current_indent,
                                                                                          self.force_next_indent),
                                 (' ' * self.force_next_indent) + line.lstrip())
            else:
                if not stripped_line.endswith('\\'):
                    self.force_next_indent = 0
                return

        else:
            if stripped_line.endswith('\\') and stripped_line.startswith(('config', 'menuconfig', 'choice')):
                raise InputError(self.path_in_idf, line_number,
                                 'Line-wrap with backslash is not supported here',
                                 line)  # no suggestion for this

        self.check_name_and_update_prefix(stripped_line, line_number)

        m = self.re_increase_level.search(line)
        if m:
            current_level = self.update_level_for_inc_pattern(m.group(1))
        else:
            m = self.re_decrease_level.search(line)
            if m:
                new_item = m.group(1)
                current_level = self.update_level_for_dec_pattern(new_item)
                if new_item not in ['endif']:
                    # endif doesn't require to check the prefix because the items in inside if/endif belong to the
                    # same prefix level
                    self.check_common_prefix(line, line_number)

        expected_indent = current_level * SPACES_PER_INDENT

        if stripped_line.endswith('\\'):
            self.force_next_indent = expected_indent + SPACES_PER_INDENT
        else:
            self.force_next_indent = 0

        if current_indent != expected_indent:
            raise InputError(self.path_in_idf, line_number,
                             'Indentation consists of {} spaces instead of {}'.format(current_indent, expected_indent),
                             (' ' * expected_indent) + line.lstrip())


def valid_directory(path):
    if not os.path.isdir(path):
        raise argparse.ArgumentTypeError("{} is not a valid directory!".format(path))
    return path


def main():
    default_path = os.getenv('IDF_PATH', None)

    parser = argparse.ArgumentParser(description='Kconfig style checker')
    parser.add_argument('--verbose', '-v', help='Print more information (useful for debugging)',
                        action='store_true', default=False)
    parser.add_argument('--directory', '-d', help='Path to directory where Kconfigs should be recursively checked '
                        '(for example $IDF_PATH)',
                        type=valid_directory,
                        required=default_path is None,
                        default=default_path)
    args = parser.parse_args()

    success_couter = 0
    ignore_counter = 0
    failure = False

    # IGNORE_DIRS makes sense when the required directory is IDF_PATH
    check_ignore_dirs = default_path is not None and os.path.abspath(args.directory) == os.path.abspath(default_path)

    for root, dirnames, filenames in os.walk(args.directory):
        for filename in [f for f in filenames if RE_KCONFIG.search(f)]:
            full_path = os.path.join(root, filename)
            path_in_idf = os.path.relpath(full_path, args.directory)
            if check_ignore_dirs and path_in_idf.startswith(IGNORE_DIRS):
                print('{}: Ignored'.format(path_in_idf))
                ignore_counter += 1
                continue
            suggestions_full_path = full_path + OUTPUT_SUFFIX
            with open(full_path, 'r', encoding='utf-8') as f, \
                    open(suggestions_full_path, 'w', encoding='utf-8', newline='\n') as f_o, \
                    LineRuleChecker(path_in_idf) as line_checker, \
                    IndentAndNameChecker(path_in_idf, debug=args.verbose) as indent_and_name_checker:
                try:
                    for line_number, line in enumerate(f, start=1):
                        try:
                            for checker in [line_checker, indent_and_name_checker]:
                                checker.process_line(line, line_number)
                            # The line is correct therefore we echo it to the output file
                            f_o.write(line)
                        except InputError as e:
                            print(e)
                            failure = True
                            f_o.write(e.suggested_line)
                except UnicodeDecodeError:
                    raise ValueError("The encoding of {} is not Unicode.".format(path_in_idf))

            if failure:
                print('{} has been saved with suggestions for resolving the issues. Please note that the suggestions '
                      'can be wrong and you might need to re-run the checker several times for solving all issues.'
                      ''.format(path_in_idf + OUTPUT_SUFFIX))
                print('Please fix the errors and run {} for checking the correctness of '
                      'Kconfigs.'.format(os.path.relpath(os.path.abspath(__file__), args.directory)))
                sys.exit(1)
            else:
                success_couter += 1
                print('{}: OK'.format(path_in_idf))
                try:
                    os.remove(suggestions_full_path)
                except Exception:
                    # not a serious error is when the file cannot be deleted
                    print('{} cannot be deleted!'.format(suggestions_full_path))

    if ignore_counter > 0:
        print('{} files have been ignored.'.format(ignore_counter))

    if success_couter > 0:
        print('{} files have been successfully checked.'.format(success_couter))


if __name__ == "__main__":
    main()
