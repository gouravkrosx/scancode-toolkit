#
# Copyright (c) 2018 nexB Inc. and others. All rights reserved.
# http://nexb.com and https://github.com/nexB/scancode-toolkit/
# The ScanCode software is licensed under the Apache License version 2.0.
# Data generated with ScanCode require an acknowledgment.
# ScanCode is a trademark of nexB Inc.
#
# You may not use this software except in compliance with the License.
# You may obtain a copy of the License at: http://apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
# CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.
#
# When you publish or redistribute any data created with ScanCode or any ScanCode
# derivative work, you must accompany this data with the following acknowledgment:
#
#  Generated with ScanCode and provided on an "AS IS" BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, either express or implied. No content created from
#  ScanCode should be considered or used as legal advice. Consult an Attorney
#  for any legal advice.
#  ScanCode is a free software code scanning tool from nexB Inc. and others.
#  Visit https://github.com/nexB/scancode-toolkit/ for support and download.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from collections import Counter
from collections import defaultdict
from collections import namedtuple
from collections import OrderedDict
from functools import partial
from itertools import chain
import io
from operator import itemgetter
from os.path import abspath
from os.path import dirname
from os.path import exists
from os.path import join
import traceback

import attr
from license_expression import Licensing

from commoncode.fileutils import copyfile
from commoncode.fileutils import file_base_name
from commoncode.fileutils import file_name
from commoncode.fileutils import resource_iter
from commoncode import saneyaml
from textcode.analysis import numbered_text_lines

from licensedcode import MIN_MATCH_LENGTH
from licensedcode import MIN_MATCH_HIGH_LENGTH
from licensedcode.tokenize import query_tokenizer

# these are globals but always side-by-side with the code so not moving
data_dir = join(abspath(dirname(__file__)), 'data')
licenses_data_dir = join(data_dir, 'licenses')
rules_data_dir = join(data_dir, 'rules')

"""
Reference License and license Rule structures persisted as a combo of a YAML
data file and one or more text files containing license or notice texts.
"""

# Set to True to print detailed representations of objects when tracing
TRACE_REPR = False


FOSS_CATEGORIES = set([
    'Copyleft',
    'Copyleft Limited',
    'Patent License',
    'Permissive',
    'Public Domain',
])


OTHER_CATEGORIES = set([
    'Commercial',
    'Free Restricted',
    'Proprietary Free',
    'Unstated License',
])


CATEGORIES = FOSS_CATEGORIES | OTHER_CATEGORIES


@attr.s(slots=True)
class License(object):
    """
    A license consists of these files, where <key> is the license key:
        - <key>.yml : the license data in YAML
        - <key>.LICENSE: the license text

    A License object is identified by a unique `key` and its data stored in the
    `src_dir` directory. Key is a lower-case unique ascii string.
    """

    __attrib = partial(attr.ib, repr=False)

    # unique key: lower case ASCII characters, digits, underscore and dots.
    key = attr.ib(default=None, repr=True)

    src_dir = __attrib(default=licenses_data_dir)

    # if this is a deprecated license, add also notes explaining why
    is_deprecated = __attrib(default=False)

    # if this license text is not in English, set this field to a two letter
    # ISO 639-1 language code https://en.wikipedia.org/wiki/ISO_639-1
    # NOTE: this is not yet supported.
    # NOTE: each translation of a license text MUST have a different license key
    language = __attrib(default='en')

    # commonly used short name, often abbreviated.
    short_name = __attrib(default=None)
    # full name.
    name = __attrib(default=None)

    # Permissive, Copyleft, etc
    category = __attrib(default=None)

    owner = __attrib(default=None)
    homepage_url = __attrib(default=None)
    notes = __attrib(default=None)

    # if this is a license exception, the license key this exception applies to
    is_exception = __attrib(default=False)

    # SPDX key for SPDX licenses
    spdx_license_key = __attrib(default=None)
    # list of other keys, such as deprecated ones
    other_spdx_license_keys = __attrib(default=attr.Factory(list))

    # Various URLs for info
    text_urls = __attrib(default=attr.Factory(list))
    osi_url = __attrib(default=None)
    faq_url = __attrib(default=None)
    other_urls = __attrib(default=attr.Factory(list))

    # various alternate keys for this license
    key_aliases = __attrib(default=attr.Factory(list))

    minimum_coverage = __attrib(default=0)
    relevance = __attrib(default=100)
    standard_notice = __attrib(default=None)

    # data file paths and known extensions
    data_file = __attrib(default=None)
    text_file = __attrib(default=None)

    def __attrs_post_init__(self, *args, **kwargs):

        if self.src_dir:
            self.set_file_paths()

            if exists(self.data_file):
                self.load()

    def set_file_paths(self):
        self.data_file = join(self.src_dir, self.key + '.yml')
        self.text_file = join(self.src_dir, self.key + '.LICENSE')

    def relocate(self, target_dir, new_key=None):
        """
        Return f copy of this license object relocated to f new `src_dir`.
        The data and license text files are persisted in the new `src_dir`.
        """
        if not target_dir or target_dir == self.src_dir:
            raise ValueError(
                'Cannot relocate f License to empty directory or same directory.')

        if new_key:
            key = new_key
        else:
            key = self.key

        newl = License(key, target_dir)

        # copy fields
        excluded_fields = ('key', 'src_dir', 'data_file', 'text_file',)
        all_fields = attr.fields(self.__class__)
        attrs = [f.name for f in all_fields if f.name not in excluded_fields]
        for name in attrs:
            setattr(newl, name, getattr(self, name))

        # save it all to files
        if self.text:
            copyfile(self.text_file, newl.text_file)
        newl.dump()
        return newl

    def update(self, mapping):
        for k, v in mapping.items():
            setattr(self, k, v)

    def __copy__(self):
        oldl = self.to_dict()
        newl = License(key=self.key)
        newl.update(oldl)
        return newl

    @property
    def text(self):
        """
        License text, re-loaded on demand.
        """
        return self._read_text(self.text_file)

    def to_dict(self):
        """
        Return an OrderedDict of license data (excluding texts).
        Fields with empty values are not included.
        """

        # do not dump false, empties and paths
        def dict_fields(attr, value):
            if not value:
                return False

            if attr.name in ('data_file', 'text_file', 'src_dir',):
                return False

            # default to English
            if attr.name == 'language' and value == 'en':
                return False

            if attr.name == 'relevance' and value == 100:
                return False

            return True

        return attr.asdict(self, filter=dict_fields, dict_factory=OrderedDict)

    def dump(self):
        """
        Dump a representation of this license as two files:
         - <key>.yml : the license data in YAML
         - <key>.LICENSE: the license text
        """

        def write(location, byte_string):
            # we write as binary because rules and licenses texts and data are UTF-8-encoded bytes
            with io.open(location, 'wb') as of:
                of.write(byte_string)

        as_yaml = saneyaml.dump(self.to_dict(), indent=4, encoding='utf-8')
        write(self.data_file, as_yaml)
        if self.text:
            write(self.text_file, self.text.encode('utf-8'))

    def load(self):
        """
        Populate license data from a YAML file stored in of self.src_dir.
        Does not load text files.
        Unknown fields are ignored and not bound to the License object.
        """
        try:
            with io.open(self.data_file, encoding='utf-8') as f:
                data = saneyaml.load(f.read())

            numeric_keys = ('minimum_coverage', 'relevance')
            for k, v in data.items():
                if k in numeric_keys:
                    v = int(v)

                if k == 'key':
                    assert self.key == v, 'Inconsistent YAML key and file names for %r' % self.key

                setattr(self, k, v)

        except Exception as e:
            # this is a rare case: fail loudly
            print()
            print('#############################')
            print('INVALID LICENSE YAML FILE:', 'file://' + self.data_file)
            print('#############################')
            print(e)
            print('#############################')
            raise

    def _read_text(self, location):
        if not exists(location):
            text = ''
        else:
            with io.open(location, encoding='utf-8') as f:
                text = f.read()
        return text

    def spdx_keys(self):
        """
        Yield SPDX keys for this license.
        """
        if self.spdx_license_key:
            yield self.spdx_license_key
        for key in self.other_spdx_license_keys:
            yield key

    @staticmethod
    def validate(licenses, verbose=False, no_dupe_urls=False):
        """
        Check that licenses are valid. `licenses` is a mapping of key ->
        License. Return dictionaries of infos, errors and warnings mapping a
        license key to validation issue messages. Print messages if verbose is
        True.

        NOTE: we DO NOT run this validation as part of the loading or
        construction of License objects. Instead this is invoked ONLY as part of
        the test suite.
        """
        infos = defaultdict(list)
        warnings = defaultdict(list)
        errors = defaultdict(list)

        # used for global dedupe of texts
        by_spdx_key = defaultdict(list)
        by_text = defaultdict(list)
        by_short_name = defaultdict(list)
        by_name = defaultdict(list)

        for key, lic in licenses.items():
            warn = warnings[key].append
            info = infos[key].append
            error = errors[key].append
            by_name[lic.name].append(lic)
            by_short_name[lic.short_name].append(lic)

            if not lic.short_name:
                error('No short name')
            if not lic.name:
                error('No name')
            if not lic.category:
                error('No category')
            if lic.category and lic.category not in CATEGORIES:
                error('Unknown license category: {}'.format(lic.category))
            if not lic.owner:
                error('No owner')

            # URLS dedupe and consistency
            if no_dupe_urls:
                if lic.text_urls and not all(lic.text_urls):
                    warn('Some empty text_urls values')

                if lic.other_urls and not all(lic.other_urls):
                    warn('Some empty other_urls values')

                # redundant URLs used multiple times
                if lic.homepage_url:
                    if lic.homepage_url in lic.text_urls:
                        warn('Homepage URL also in text_urls')
                    if lic.homepage_url in lic.other_urls:
                        warn('Homepage URL also in other_urls')
                    if lic.homepage_url == lic.faq_url:
                        warn('Homepage URL same as faq_url')
                    if lic.homepage_url == lic.osi_url:
                        warn('Homepage URL same as osi_url')

                if lic.osi_url or lic.faq_url:
                    if lic.osi_url == lic.faq_url:
                        warn('osi_url same as faq_url')

                all_licenses = lic.text_urls + lic.other_urls
                for url in lic.osi_url, lic.faq_url, lic.homepage_url:
                    if url:
                        all_licenses.append(url)

                if not len(all_licenses) == len(set(all_licenses)):
                    warn('Some duplicated URLs')

            # local text consistency
            text = lic.text

            license_qtokens = tuple(query_tokenizer(text, lower=True))
            if not license_qtokens:
                info('No license text')
            else:
                # for global dedupe
                by_text[license_qtokens].append(key + ': TEXT')

            # SPDX consistency
            if lic.spdx_license_key:
                by_spdx_key[lic.spdx_license_key].append(key)
            for oslk in lic.other_spdx_license_keys:
                by_spdx_key[oslk].append(key)

        # global SPDX consistency
        multiple_spdx_keys_used = {k: v for k, v in by_spdx_key.items() if len(v) > 1}
        if multiple_spdx_keys_used:
            for k, lkeys in multiple_spdx_keys_used.items():
                errors['GLOBAL'].append('SPDX key: ' + k + ' used in multiple licenses: ' + ', '.join(sorted(lkeys)))

        # global text dedupe
        multiple_texts = {k: v for k, v in by_text.items() if len(v) > 1}
        if multiple_texts:
            for k, msgs in multiple_texts.items():
                errors['GLOBAL'].append('Duplicate texts in multiple licenses:' + ', '.join(sorted(msgs)))

        # global short_name dedupe
        for short_name, licenses in by_short_name.items():
            if len(licenses) == 1:
                continue
            errors['GLOBAL'].append('Duplicate short name:' + short_name + ' in licenses:' + ', '.join(l.key for l in licenses))

        # global name dedupe
        for name, licenses in by_name.items():
            if len(licenses) == 1:
                continue
            errors['GLOBAL'].append('Duplicate name:' + name + ' in licenses:' + ', '.join(l.key for l in licenses))

        errors = {k: v for k, v in errors.items() if v}
        warnings = {k: v for k, v in warnings.items() if v}
        infos = {k: v for k, v in infos.items() if v}

        if verbose:
            print('Licenses validation errors:')
            for key, msgs in sorted(errors.items()):
                print('ERRORS for:', key, ':', '\n'.join(msgs))

            print('Licenses validation warnings:')
            for key, msgs in sorted(warnings.items()):
                print('WARNINGS for:', key, ':', '\n'.join(msgs))

            print('Licenses validation infos:')
            for key, msgs in sorted(infos.items()):
                print('INFOS for:', key, ':', '\n'.join(msgs))

        return errors, warnings, infos


def load_licenses(licenses_data_dir=licenses_data_dir , with_deprecated=False):
    """
    Return a mapping of key -> license objects, loaded from license files.
    Raise Exceptions if there are dangling orphaned files.
    """
    licenses = {}
    used_files = set()
    all_files = set(resource_iter(licenses_data_dir, with_dirs=False))
    for data_file in sorted(all_files):
        if data_file.endswith('.yml'):
            key = file_base_name(data_file)
            lic = License(key, licenses_data_dir)
            used_files.add(data_file)
            if exists(lic.text_file):
                used_files.add(lic.text_file)
            if not with_deprecated and lic.is_deprecated:
                continue
            licenses[key] = lic

    dangling = all_files.difference(used_files)
    if dangling:
        msg = 'Some License data or text files are orphaned in "{}".\n'.format(licenses_data_dir)
        msg += '\n'.join('file://{}'.format(f) for f in sorted(dangling))
        raise Exception(msg)
    return licenses


def get_rules(licenses_data_dir=licenses_data_dir, rules_data_dir=rules_data_dir):
    """
    Return a mapping of key->license and an iterable of license detection rules
    loaded from licenses and rules files.
    Raise a MissingLicenses exceptions if a rule references unknown license key.
    """
    from licensedcode.cache import get_licenses_db
    licenses = get_licenses_db(licenses_data_dir=licenses_data_dir)
    rules = list(load_rules(rules_data_dir=rules_data_dir))
    check_rules_integrity(rules, licenses)
    licenses_as_rules = build_rules_from_licenses(licenses)
    return chain(licenses_as_rules, rules)


class MissingLicenses(Exception):
    pass

class MissingFlags(Exception):
    pass


def check_rules_integrity(rules, licenses_by_key):
    """
    Given a lists of `rules`, check that all the rule license keys reference a
    known license from a mapping of `licenses_by_key `(key->license). Raise a
    MissingLicense exception with a message containing the list of rule files
    without a corresponding license.
    """
    invalid_rules = defaultdict(set)
    rules_without_flags = set()
    for rule in rules:
        unknown_keys = [key for key in rule.license_keys()
                        if key not in licenses_by_key]
        if unknown_keys:
            invalid_rules[rule.data_file].update(unknown_keys)

        if not rule.has_importance_flags and not (rule.is_negative or rule.is_false_positive):
            rules_without_flags.add(rule.data_file)

    if invalid_rules:
        invalid_rules = (
            ' '.join(keys) + '\n' +
            'file://' + data_file + '\n' +
            'file://' + data_file.replace('.yml', '.RULE') + '\n'
        for data_file, keys in invalid_rules.iteritems() if keys)
        msg = 'Rules referencing missing licenses:\n' + '\n'.join(sorted(invalid_rules))
        raise MissingLicenses(msg)

    if rules_without_flags:
        invalid_rules = (
            'file://' + data_file + '\n' +
            'file://' + data_file.replace('.yml', '.RULE') + '\n'
        for data_file in sorted(rules_without_flags))
        msg = 'Rules without is_license_xxx flags:\n' + '\n'.join(sorted(invalid_rules))
        raise MissingFlags(msg)


def build_rules_from_licenses(licenses):
    """
    Return an iterable of rules built from each license text from a `licenses`
    iterable of license objects.
    """
    for license_key, license_obj in licenses.iteritems():
        text_file = join(license_obj.src_dir, license_obj.text_file)
        minimum_coverage = license_obj.minimum_coverage or 0
        has_stored_relevance = license_obj.relevance != 100
        relevance = license_obj.relevance or 100

        if exists(text_file):
            yield Rule(text_file=text_file,
                       license_expression=license_key,
                       minimum_coverage=minimum_coverage,
                       relevance=relevance,
                       has_stored_relevance=has_stored_relevance,
                       is_license=True,
                       is_license_text=True)


def get_all_spdx_keys(licenses):
    """
    Return an iterable of SPDX license keys collected from a `licenses` iterable
    of license objects.
    """
    for lic in licenses.viewvalues():
        for spdx_key in lic.spdx_keys():
            yield spdx_key


def get_essential_spdx_tokens():
    """
    Yield essential SPDX tokens.
    """
    yield 'spdx'
    yield 'license'
    yield 'identifier'
    yield 'licenseref'


def get_all_spdx_key_tokens(licenses):
    """
    Yield token strings collected from a `licenses` iterable of license objects'
    SPDX license keys.
    """
    for tok in get_essential_spdx_tokens():
        yield tok

    for spdx_key in get_all_spdx_keys(licenses):
        for token in query_tokenizer(spdx_key):
            yield token


def load_rules(rules_data_dir=rules_data_dir):
    """
    Return an iterable of rules loaded from rule files.
    """
    # TODO: OPTIMIZE: create a graph of rules to account for containment and
    # similarity clusters?
    seen_files = set()
    processed_files = set()
    lower_case_files = set()
    case_problems = set()
    space_problems = []
    model_errors = []
    for data_file in resource_iter(rules_data_dir, with_dirs=False):
        if data_file.endswith('.yml'):
            base_name = file_base_name(data_file)
            if ' ' in base_name:
                space_problems.append(data_file)
            rule_file = join(rules_data_dir, base_name + '.RULE')
            try:
                rule = Rule(data_file=data_file, text_file=rule_file)
                yield rule
            except Exception as re:
                model_errors.append(str(re))
            # accumulate sets to ensures we do not have illegal names or extra
            # orphaned files
            data_lower = data_file.lower()
            if data_lower in lower_case_files:
                case_problems.add(data_lower)
            else:
                lower_case_files.add(data_lower)

            rule_lower = rule_file.lower()
            if rule_lower in lower_case_files:
                case_problems.add(rule_lower)
            else:
                lower_case_files.add(rule_lower)

            processed_files.update([data_file, rule_file])

        if not data_file.endswith('~'):
            seen_files.add(data_file)


    unknown_files = seen_files - processed_files
    if unknown_files or case_problems or model_errors or space_problems:
        msg = ''

        if model_errors:
            errors = '\n'.join(model_errors)
            msg += '\nInvalid rule YAML in directory: %(rules_data_dir)r\n%(errors)s' % locals()

        if unknown_files:
            files = '\n'.join(sorted('file://' + f for f in unknown_files))
            msg += '\nOrphaned files in rule directory: %(rules_data_dir)r\n%(files)s' % locals()

        if case_problems:
            files = '\n'.join(sorted('file://' + f for f in case_problems))
            msg += '\nRule files with non-unique name ignoring casein rule directory: %(rules_data_dir)r\n%(files)s' % locals()

        if space_problems:
            files = '\n'.join(sorted('"file://' + f + '"' for f in space_problems))
            msg += '\nRule files name cannot contain spaces: %(rules_data_dir)r\n%(files)s' % locals()

        raise Exception(msg)


# a set of thresholds for this rule to determine when a match should be treated
# as a good match
# TODO: use attr instead
Thresholds = namedtuple(
    'Thresholds',
    [
        # number of "high" tokens in this rule
        'high_len',
        # number of "low" tokens in this rule
        'low_len',
        # length of the rule
        'length',
        # boolean set to True from Rule.small()
        'small',
        # minimum number of "high" tokens to match for this rule
        'min_high',
        # minimum number of tokens to match for this rule
        'min_len'
    ]
)


@attr.s(slots=True)
class Rule(object):
    """
    A detection rule object is a text to use for detection and corresponding
    detected licenses and metadata.
    """
    licensing = Licensing()

    ###########
    # FIXME: !!! TWO RULES MAY DIFFER BECAUSE THEY ARE UPDATED BY INDEXING
    ###########

    # optional rule id int typically assigned at indexing time
    rid = attr.ib(default=None, repr=False)

    # unique identifier
    identifier = attr.ib(default=None)

    # License expression string
    license_expression = attr.ib(default=None)

    # License expression object, created at build time
    license_expression_object = attr.ib(default=None, repr=False)

    # an indication of what this rule importance is (e.g. how important is its
    # text when detected as a licensing clue) as one of several flags:

    # for a license full text: this provides the highest level of confidence wrt
    # detection
    is_license_text = attr.ib(default=False, repr=False)

    # for a license notice: this provides a strong confidence wrt detection
    is_license_notice = attr.ib(default=False, repr=False)

    # reference for a mere short license reference such as its bare name or a URL
    # this provides a weak confidence wrt detection
    is_license_reference = attr.ib(default=False, repr=False)

    # tag for a structured licensing tag such as a package manifest metadata or
    # an SPDX license identifier or similar package manifest tag
    # this provides a strong confidence wrt detection
    is_license_tag = attr.ib(default=False, repr=False)

    # is this rule text a false positive when matched? (filtered out) FIXME: this
    # should be unified with the relevance: a false positive match is a a match
    # with a relevance of zero
    is_false_positive = attr.ib(default=False)

    is_negative = attr.ib(default=False)

    # is this rule text only to be matched with a minimum coverage?
    minimum_coverage = attr.ib(default=0)

    # Can this rule be matched if there are unknown words in its matched range?
    # The default is to allow known and unknown words. Unknown words are words
    # that do not exist in the text of any indexed license or license detection
    # rule.
    only_known_words = attr.ib(default=False)

    # what is the relevance of a match to this rule text? a float between 0 and
    # 100 where 100 means highly relevant and 0 menas not relevant at all.
    # For instance a match to the "gpl" or the "cpol" words have a fairly low
    # relevance as they are a weak indication of an actual license and could be
    # a false positive. In somce cases, this may even be used to discard obvious
    # false positive matches automatically.
    relevance = attr.ib(default=100)
    has_stored_relevance = attr.ib(default=False, repr=False)

    # The rule contains a reference to some file name that comtains the text
    referenced_filenames = attr.ib(default=attr.Factory(list), repr=False)

    # optional, free text
    notes = attr.ib(default=None, repr=False)

    # set to True if the rule is built from a .LICENSE full text
    is_license = attr.ib(default=False, repr=False)

    # path to the YAML data file for this rule
    data_file = attr.ib(default=None, repr=False)

    # path to the rule text file
    text_file = attr.ib(default=None, repr=False)

    # text of this rule for special cases where the rule is not backed by a file:
    # for SPDX license expression dynamic rules or testing
    stored_text = attr.ib(default=None, repr=False)

    # These attributes are computed upon text loading or setting the thresholds
    ###########################################################################

    # length in number of token strings
    length = attr.ib(default=0, repr=False)

    # lengths in token ids, including high/low token counts, set in indexing.
    # This considers the all tokens occurences
    high_length = attr.ib(default=0, repr=False)
    low_length = attr.ib(default=0, repr=False)
    _thresholds = attr.ib(default=None, repr=False)

    # lengths in token ids, including high/low token counts, set in indexing.
    # This considers the unique tokens (ignoring multiple occurences)
    high_unique = attr.ib(default=0, repr=False)
    low_unique = attr.ib(default=0, repr=False)
    length_unique = attr.ib(default=0, repr=False)
    _thresholds_unique = attr.ib(default=None, repr=False)

    def __attrs_post_init__(self, *args, **kwargs):
        if not self.text_file:
            # for SPDX or tests only
            if not self.stored_text :
                raise Exception('Invalid rule without its corresponding text file: {}'.format(self))
            self.identifier = '_tst_' + str(len(self.stored_text))
        else:
            self.identifier = file_name(self.text_file)

        if self.data_file:
            try:
                self.load()
            except Exception as e:
                data_file = self.data_file
                trace = traceback.format_exc()
                message = 'While loading: file://{data_file}\n{trace}'.format(**locals())
                raise Exception(message)

        if self.relevance and self.relevance != 100:
            self.has_stored_relevance = True

        if self.license_expression:
            try:
                expression = self.licensing.parse(self.license_expression)
            except:
                raise Exception(
                    'Unable to parse License rule expression: '
                    +repr(self.license_expression) + ' for: file://' + self.data_file +
                    '\n' + traceback.format_exc()
                )
            if expression is None:
                raise Exception(
                    'Unable to parse License rule expression: '
                    +repr(self.license_expression) + ' for: file://' + self.data_file)

            self.license_expression = expression.render()
            self.license_expression_object = expression

    def tokens(self, lower=True):
        """
        Return an iterable of token strings for this rule. Length, relevance and
        minimum_coverage may be recomputed as a side effect.
        """
        length = 0
        text = self.text()
        text = text.strip()

        # FIXME: this is weird:

        # We tag this rule as being a bare URL if it starts with a scheme and is
        # on one line: this is used to determine a matching approach

        # FIXME: this does not lower the text first??
        if text.startswith(('http://', 'https://', 'ftp://')) and '\n' not in text[:1000]:
            self.minimum_coverage = 100

        for token in query_tokenizer(self.text(), lower=lower):
            length += 1
            yield token

        self.length = length
        self.compute_relevance()

    def text(self):
        """
        Return the rule text loaded from its file.
        """
        if self.text_file and exists(self.text_file):
            # IMPORTANT: use the same process as query text loading for symmetry
            numbered_lines = numbered_text_lines(self.text_file, demarkup=False, plain_text=True)
            return ''.join(l for _, l in numbered_lines)

        # used for non-file backed rules
        elif self.stored_text:
            return self.stored_text

        else:
            raise Exception('Inconsistent rule text for: ' + self.identifier + '\nfile://' + self.text_file)

    def license_keys(self, unique=True):
        """
        Return a list of license keys for this rule.
        """
        if not self.license_expression:
            return []
        return self.licensing.license_keys(self.license_expression_object, unique=unique)

    def same_licensing(self, other):
        """
        Return True if the other rule has a the same licensing as this rule.
        """
        if self.license_expression and other.license_expression:
            return self.licensing.is_equivalent(
                self.license_expression_object, other.license_expression_object)

    def licensing_contains(self, other):
        """
        Return True if this rule licensing contains the other rule licensing.
        """
        if self.license_expression and other.license_expression:
            return self.licensing.contains(
                self.license_expression_object, other.license_expression_object)

    def small(self):
        """
        Is this a small rule? It needs special handling for detection.
        """
        SMALL_RULE = 15
        return self.length < SMALL_RULE or self.minimum_coverage == 100

    def thresholds(self):
        """
        Return a Thresholds tuple considering the occurrence of all tokens.
        """
        if not self._thresholds:
            length = self.length
            high_length = self.high_length
            if length > 200:
                min_high = high_length // 10
                min_len = length // 10
            else:
                min_high = min([high_length, MIN_MATCH_HIGH_LENGTH])
                min_len = MIN_MATCH_LENGTH

            # note: we cascade ifs from largest to smallest lengths
            # FIXME: this is not efficient

            if self.length < 30:
                min_len = length // 2

            if self.length < 10:
                min_high = high_length
                min_len = length
                self.minimum_coverage = 80

            if self.length < 3:
                min_high = high_length
                min_len = length
                self.minimum_coverage = 100

            if self.minimum_coverage == 100:
                min_high = high_length
                min_len = length

            self._thresholds = Thresholds(
                high_length, self.low_length, length,
                self.small(), min_high, min_len
            )
        return self._thresholds

    def thresholds_unique(self):
        """
        Return a Thresholds tuple considering the occurrence of only unique tokens.
        """
        if not self._thresholds_unique:
            length = self.length
            high_unique = self.high_unique
            length_unique = self.length_unique

            if length > 200:
                min_high = high_unique // 10
                min_len = length // 10
            else:
                highu = (int(high_unique // 2)) or high_unique
                min_high = min([highu, MIN_MATCH_HIGH_LENGTH])
                min_len = MIN_MATCH_LENGTH

            # note: we cascade IFs from largest to smallest lengths
            if length < 20:
                min_high = high_unique
                min_len = min_high

            if length < 10:
                min_high = high_unique
                if length_unique < 2:
                    min_len = length_unique
                else:
                    min_len = length_unique - 1

            if length < 5:
                min_high = high_unique
                min_len = length_unique

            if self.minimum_coverage == 100:
                min_high = high_unique
                min_len = length_unique

            self._thresholds_unique = Thresholds(
                high_unique, self.low_unique, length_unique,
                self.small(), min_high, min_len)
        return self._thresholds_unique

    def to_dict(self):
        """
        Return an ordered mapping of self, excluding texts. Used for
        serialization. Empty values are not included.
        """
        data = OrderedDict()
        if self.license_expression:
            data['license_expression'] = self.license_expression

        flags = (
            'is_false_positive',
            'is_negative',
            'is_license_text',
            'is_license_notice',
            'is_license_reference',
            'is_license_tag',
            'only_known_words',
        )

        for flag in flags:
            tag_value = getattr(self, flag, False)
            if tag_value:
                data[flag] = tag_value

        if self.has_stored_relevance:
            rl = self.relevance
            if int(rl) == rl:
                rl = int(rl)
            data['relevance'] = rl

        if self.minimum_coverage:
            mc = self.minimum_coverage
            if int(mc) == mc:
                mc = int(mc)
            data['minimum_coverage'] = mc

        if self.referenced_filenames:
            data['referenced_filenames'] = self.referenced_filenames

        if self.notes:
            data['notes'] = self.notes
        return data

    def dump(self):
        """
        Dump a representation of this rule as two files:
         - a .yml for the rule data in YAML (self.data_file)
         - a .RULE: the rule text as a UTF-8 file (self.text_file)
        Does nothing if this rule was created a from a License (e.g.
        `is_license` is True)
        """
        if self.is_license:
            return

        def write(location, byte_string):
            # we write as binary because rules and licenses texts and data are UTF-8-encoded bytes
            with io.open(location, 'wb') as of:
                of.write(byte_string)

        if self.data_file:
            as_yaml = saneyaml.dump(self.to_dict(), indent=4, encoding='utf-8')
            write(self.data_file, as_yaml)
            write(self.text_file, self.text().encode('utf-8'))

    def load(self):
        """
        Load self from a .RULE YAML file stored in self.data_file.
        Does not load the rule text file.
        Unknown fields are ignored and not bound to the Rule object.
        """
        try:
            with io.open(self.data_file, encoding='utf-8') as f:
                data = saneyaml.load(f.read())
        except Exception as e:
            print('#############################')
            print('INVALID LICENSE RULE FILE:', 'file://' + self.data_file)
            print('#############################')
            print(e)
            print('#############################')
            # this is a rare case, but yes we abruptly stop.
            raise e
        known_attributes = set(attr.fields_dict(self.__class__))
        data_file_attributes = set(data)
        unknown_attributes = data_file_attributes.difference(known_attributes)
        if unknown_attributes:
            unknown_attributes = ', '.join(sorted(unknown_attributes))
            msg = 'License rule {} data file has unknown attributes: {}'
            raise Exception(msg.format(self, unknown_attributes))

        self.license_expression = data.get('license_expression')
        self.is_negative = data.get('is_negative', False)
        self.is_false_positive = data.get('is_false_positive', False)

        if not self.license_expression and not (self.is_negative or self.is_false_positive):
            msg = 'License rule {} is missing a license_expression.'
            raise Exception(msg.format(self))

        relevance = data.get('relevance')
        if relevance is not None:
            # Keep track if we have a stored relevance of not.
            self.has_stored_relevance = True
            self.relevance = float(relevance)
            if not (0 < self.relevance <= 100):
                msg = (
                    'License rule {} data file has an invalid relevance. '
                    'Should be between 0 and 100: {}')
                raise Exception(msg.format(self, unknown_attributes))

        self.minimum_coverage = float(data.get('minimum_coverage', 0))

        if not (0 <= self.relevance <= 100):
            msg = (
                'License rule {} data file has an invalid minimum_coverage. '
                'Should be between 0 and 100: {}')
            raise Exception(msg.format(self, self.minimum_coverage))

        self.is_license_text = data.get('is_license_text', False)
        self.is_license_notice = data.get('is_license_notice', False)
        self.is_license_tag = data.get('is_license_tag', False)
        self.is_license_reference = data.get('is_license_reference', False)
        self.only_known_words = data.get('only_known_words', False)
        self.referenced_filenames = data.get('referenced_filenames', []) or []
        if not isinstance(self.referenced_filenames, list):
            msg = (
                'License rule {} data file has an invalid referenced_filenames. '
                'Should be a list: {}')
            raise Exception(msg.format(self, self.referenced_filenames))

        # these are purely informational and not used at run time
        notes = data.get('notes')
        if notes:
            self.notes = notes.strip()

        if not self.notes and (self.is_negative or self.is_false_positive):
            msg = 'Special License rule {} is missing explanatory notes.'
            raise Exception(msg.format(self))

        return self

    def compute_relevance(self):
        """
        Compute and set the `relevance` attribute for this rule. The
        relevance is a float between 0 and 100 where 100 means highly
        relevant and 0 means not relevant at all.

        For instance a match to the "gpl" or the "cpol" words have a fairly low
        relevance as they are a weak indication of an actual license and could be a
        false positive and should therefore be assigned a low relevance. In contrast
        a match to most or all of the apache-2.0 license text is highly relevant. The
        Rule relevance is used as the basis to compute a match score.

        The relevance is either pre-defined in the rule YAML data file with the
        "relevance" attribute or computed base on the rule length here using
        this approach:

        - a false positive or a negative rule has a relevance of zero.
        - a rule of length equal to or larger than a threshold has a 100 relevance
        - a rule of length smaller than a threshold has a relevance of
          100/threshold, rounded down.

        The current threshold is 18 words.
        """
        if self.has_stored_relevance:
            return

        # case for false positive: they do not have licenses and their matches are
        # never returned. Relevance is zero.
        if self.is_false_positive:
            self.relevance = 100
            return

        # case for negative rules with no license (and are not an FP)
        # they do not have licenses and their matches are never returned
        if self.is_negative:
            self.relevance = 100
            return

        threshold = 18.0
        relevance_of_one_word = round((1 / threshold) * 100, 2)
        length = self.length
        if length >= threshold:
            # general case
            self.relevance = 100
        else:
            computed = int(length * relevance_of_one_word)
            self.relevance = min([100, computed])

    @property
    def has_importance_flags(self):
        """
        Return True if this Rule has at least one "importance" flag set.
        Needed as a temporary helper during setting importance flags.
        """
        return (
            self.is_license_text
            or self.is_license_notice
            or self.is_license_reference
            or self.is_license_tag
        )


@attr.s(slots=True, repr=False)
class SpdxRule(Rule):
    """
    A specialized rule object that is used for the special case of SPDX license
    expressions.

    Since we may have an infinite possible number of SPDX expressions and these
    are not backed by a traditional rule text file, we use this class to handle
    the specifics of these how rules that are built at matching time.
    """

    def __attrs_post_init__(self, *args, **kwargs):
        self.identifier = 'spdx-license-identifier: ' + self.license_expression
        self.has_stored_relevance = True
        self.relevance = 100

        try:
            expression = self.licensing.parse(self.license_expression)
        except:
            raise Exception(
                'Unable to parse License rule expression: ' +
                repr(self.license_expression) + ' for: file://' +
                (self.data_file or '') +
                '\n' + traceback.format_exc()
            )
        if expression is None:
            raise Exception(
                'Unable to parse License rule expression: '
                +repr(self.license_expression) + ' for:' + repr(self.data_file))

        self.license_expression = expression.render()
        self.license_expression_object = expression
        self.is_license_tag = True

    def load(self):
        raise NotImplementedError

    def dump(self):
        raise NotImplementedError

    def small(self):
        return False


def _print_rule_stats():
    """
    Print rules statistics.
    """
    from licensedcode.cache import get_index
    idx = get_index()
    rules = idx.rules_by_rid
    sizes = Counter(r.length for r in rules)
    print('Top 15 lengths: ', sizes.most_common(15))
    print('15 smallest lengths: ', sorted(sizes.iteritems(),
                                          key=itemgetter(0))[:15])

    high_sizes = Counter(r.high_length for r in rules)
    print('Top 15 high lengths: ', high_sizes.most_common(15))
    print('15 smallest high lengths: ', sorted(high_sizes.iteritems(),
                                               key=itemgetter(0))[:15])
