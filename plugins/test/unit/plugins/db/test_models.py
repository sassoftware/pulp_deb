"""
Contains tests for pulp_deb.plugins.db.models
"""

from __future__ import unicode_literals

import os
from debian import deb822
# Important to import testbase, since it mocks the server's config import snafu
from .... import testbase
from pulp_deb.plugins.db import models

DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__),
                           '../../../data'))


class TestModel(testbase.TestCase):
    def test_from_file_no_metadata(self):
        pkg_path = os.path.join(DATA_DIR, "nscd_2.24-7ubuntu2_amd64.deb")
        pkg = models.DebPackage.from_file(pkg_path)
        self.assertTrue(isinstance(pkg, models.DebPackage))
        self.assertEquals(pkg.unit_key, {
            'name': 'nscd',
            'version': '2.24-7ubuntu2',
            'architecture': 'amd64',
            'checksum': '177937795c2ef5b381718aefe2981ada4e8cfe458226348d87a6f5b100a4612b',  # noqa
            'checksumtype': 'sha256',
            })
        self.assertEquals(pkg.depends, [{'name': 'lsb-base', 'version': '3.0-6', 'flag': 'GE'},
                                        {'name': 'libaudit1', 'version': '1:2.2.1', 'flag': 'GE'},
                                        {'name': 'libc6', 'version': '2.24', 'flag': 'GT'},
                                        {'name': 'libc6', 'version': '2.25', 'flag': 'LT'},
                                        {'name': 'libcap2', 'version': '1:2.10', 'flag': 'GE'},
                                        {'name': 'libselinux1', 'version': '2.0.82', 'flag': 'GE'},
                                        {'name': 'init-system-helpers', 'version': '1.18~', 'flag': 'GE'}])

    def test_from_file_different_checksumtype(self):
        metadata = dict(checksumtype='sha1',
                        checksum='fake')
        pkg_path = os.path.join(DATA_DIR, "nscd_2.24-7ubuntu2_amd64.deb")
        pkg = models.DebPackage.from_file(pkg_path, metadata)
        self.assertTrue(isinstance(pkg, models.DebPackage))
        self.assertEquals(pkg.unit_key['name'], 'nscd')
        self.assertEquals(
            'sha256',
            pkg.checksumtype)
        self.assertEquals(
            '177937795c2ef5b381718aefe2981ada4e8cfe458226348d87a6f5b100a4612b',
            pkg.checksum)

    def test_from_file_no_file(self):
        with self.assertRaises(ValueError) as cm:
            models.DebPackage.from_file('/missing-file')
        self.assertEquals(
            "[Errno 2] No such file or directory: u'/missing-file'",
            str(cm.exception))

    def test_from_file_bad(self):
        with self.assertRaises(ValueError):
            models.DebPackage.from_file(__file__)

    def test_dep_parse(self):
        # Make sure we get the same behavior out of deb822 relationship
        # parsing
        tests = [
            ('emacs | emacsen, make, debianutils (>= 1.7)',
             [
                 [{'restrictions': None, 'version': None, 'arch': None,
                   'name': 'emacs', 'archqual': None},
                  {'restrictions': None, 'version': None, 'arch': None,
                   'name': 'emacsen', 'archqual': None}],
                 [{'restrictions': None, 'version': None, 'arch': None,
                   'name': 'make', 'archqual': None}],
                 [{'restrictions': None, 'version': ('>=', '1.7'), 'arch': None,
                   'name': 'debianutils', 'archqual': None}]],
             [
                 [{'name': 'emacs'}, {'name': 'emacsen'}],
                 {'name': 'make'},
                 {'name': 'debianutils', 'version': '1.7', 'flag': 'GE'},
             ]),

            ('tcl8.4-dev [amd64], procps [!hurd-i386]',
             [
                 [{'restrictions': None, 'version': None,
                   'arch': [(True, 'amd64')],
                   'name': 'tcl8.4-dev', 'archqual': None}],
                 [{'restrictions': None, 'version': None,
                   'arch': [(False, 'hurd-i386')],
                   'name': 'procps', 'archqual': None}]],
             [
                 {'name': 'tcl8.4-dev', 'arch': ['amd64']},
                 {'name': 'procps', 'arch': ['!hurd-i386']},
             ]),

            ('texlive <stage1 !cross> <stage2>',
             [
                 [{'restrictions': [
                     [(True, 'stage1'), (False, 'cross')],
                     [(True, 'stage2')]],
                   'version': None, 'arch': None, 'name': 'texlive', 'archqual': None}]],
             [
                 {'name': 'texlive', 'restrictions': [['stage1', '!cross'],
                                                      ['stage2']]},
             ]),
        ]
        for strdep, debdep, pulpdep in tests:
            pkg = list(deb822.Packages.iter_paragraphs(
                "Depends: {}".format(strdep)))[0]
            self.assertEquals(debdep, pkg.relations['depends'])
            self.assertEquals(pulpdep, models.DependencyParser.parse(debdep))

    def test_from_metadata_depends(self):
        """ Confirm that depends string is parsed into a list of dicts """
        pkg = dict(Depends='depa, depb',
                   Version='03.13.00',
                   Package='foo',
                   Architecture='amd64',
                   checksumtype='sha256',
                   checksum='6fa7921c574d726daf3e36729a3426aa58b93145533a4b5da954fe36a3')

        pkg_cls = models.DebPackage.from_metadata(pkg)
        self.assertEqual(pkg_cls.depends, [{'name': 'depa'}, {'name': 'depb'}])

        pkg['Depends'] = 'python2.7 (>= 2.7.11-1~), libpython-stdlib (= 2.7.11-1)'
        pkg_cls = models.DebPackage.from_metadata(pkg)
        self.assertEqual(pkg_cls.depends, [{'name': 'python2.7', 'version': '2.7.11-1~', 'flag': 'GE'},
                                           {'name': 'libpython-stdlib', 'version': '2.7.11-1', 'flag': 'EQ'}])
