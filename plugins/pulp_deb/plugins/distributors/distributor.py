from gettext import gettext as _
import errno
import logging
import os
import shutil

from collections import defaultdict

from pulp.common.config import read_json_config
from pulp.plugins.util.publish_step import AtomicDirectoryPublishStep
from pulp.plugins.util.publish_step import PluginStep, UnitModelPluginStep
from pulp.plugins.distributor import Distributor

from pulp_deb.common import ids, constants
from pulp_deb.plugins.db import models
from metadata_files import (write_packages_file,
                            write_release_file,
                            gzip_compress_file,
                            bz2_compress_file,)

from . import configuration, yum_plugin_util

_logger = logging.getLogger(__name__)


CONF_FILE_PATH = 'server/plugins.conf.d/%s.json' % ids.TYPE_ID_DISTRIBUTOR


def entry_point():
    """
    Entry point that pulp platform uses to load the distributor
    :return: distributor class and its config
    :rtype:  Distributor, dict
    """
    return DebDistributor, read_json_config(CONF_FILE_PATH)


class DebDistributor(Distributor):
    @classmethod
    def metadata(cls):
        """
        Used by Pulp to classify the capabilities of this distributor. The
        following keys must be present in the returned dictionary:

        * id - Programmatic way to refer to this distributor. Must be unique
          across all distributors. Only letters and underscores are valid.
        * display_name - User-friendly identification of the distributor.
        * types - List of all content type IDs that may be published using this
          distributor.

        :return:    keys and values listed above
        :rtype:     dict
        """
        return {
            'id': ids.TYPE_ID_DISTRIBUTOR,
            'display_name': _('Deb Distributor'),
            'types': sorted(ids.SUPPORTED_TYPES)
        }

    def __init__(self):
        super(DebDistributor, self).__init__()
        self._publisher = None
        self.canceled = False

    def publish_repo(self, transfer_repo, publish_conduit, config):
        """
        Publishes the given repository.

        :param transfer_repo: metadata describing the repository
        :type  transfer_repo: pulp.plugins.model.Repository

        :param publish_conduit: provides access to relevant Pulp functionality
        :type  publish_conduit: pulp.plugins.conduits.repo_publish.RepoPublishConduit

        :param config: plugin configuration
        :type  config: pulp.plugins.config.PluginConfiguration

        :return: report describing the publish run
        :rtype:  pulp.plugins.model.PublishReport
        """
        _logger.debug('Publishing deb repository: %s' % transfer_repo.id)
        self._publisher = Publisher(transfer_repo, publish_conduit, config,
                                    plugin_type=ids.TYPE_ID_DISTRIBUTOR)
        return self._publisher.process_lifecycle()

    def cancel_publish_repo(self):
        """
        Call cancellation control hook.
        """
        _logger.debug('Canceling deb repository publish')
        self.canceled = True
        if self._publisher is not None:
            self._publisher.cancel()

    def distributor_removed(self, repo, config):
        """
        Called when a distributor of this type is removed from a repository.
        This hook allows the distributor to clean up any files that may have
        been created during the actual publishing.

        The distributor may use the contents of the working directory in cleanup.
        It is not required that the contents of this directory be deleted by
        the distributor; Pulp will ensure it is wiped following this call.

        If this call raises an exception, the distributor will still be removed
        from the repository and the working directory contents will still be
        wiped by Pulp.

        :param repo: metadata describing the repository
        :type  repo: pulp.plugins.model.Repository

        :param config: plugin configuration
        :type  config: pulp.plugins.config.PluginCallConfiguration
        """
        # remove the directories that might have been created for this repo/distributor

        repo_dir = configuration.get_master_publish_dir(
            repo, ids.TYPE_ID_DISTRIBUTOR)
        shutil.rmtree(repo_dir, ignore_errors=True)
        # remove the symlinks that might have been created for this
        # repo/distributor
        rel_path = configuration.get_repo_relative_path(repo, config)
        rel_path = rel_path.rstrip(os.sep)
        pub_dirs = [
            configuration.get_http_publish_dir(config),
            configuration.get_https_publish_dir(config),
        ]
        for pub_dir in pub_dirs:
            symlink = os.path.join(pub_dir, rel_path)
            try:
                os.unlink(symlink)
            except OSError as error:
                if error.errno != errno.ENOENT:
                    raise

    def validate_config(self, transfer_repo, config, config_conduit):
        """
        Allows the distributor to check the contents of a potential configuration
        for the given repository. This call is made both for the addition of
        this distributor to a new repository as well as updating the configuration
        for this distributor on a previously configured repository. The implementation
        should use the given repository data to ensure that updating the
        configuration does not put the repository into an inconsistent state.

        The return is a tuple of the result of the validation (True for success,
        False for failure) and a message. The message may be None and is unused
        in the success case. For a failed validation, the message will be
        communicated to the caller so the plugin should take i18n into
        consideration when generating the message.

        The related_repos parameter contains a list of other repositories that
        have a configured distributor of this type. The distributor configurations
        is found in each repository in the "plugin_configs" field.

        :param repo: metadata describing the repository to which the
                     configuration applies
        :type  repo: pulp.plugins.model.Repository

        :param config: plugin configuration instance; the proposed repo
                       configuration is found within
        :type  config: pulp.plugins.config.PluginCallConfiguration

        :param config_conduit: Configuration Conduit;
        :type  config_conduit: pulp.plugins.conduits.repo_config.RepoConfigConduit

        :return: tuple of (bool, str) to describe the result
        :rtype:  tuple

        :raises: PulpCodedValidationException if any validations failed
        """
        repo = transfer_repo.repo_obj
        return configuration.validate_config(repo, config, config_conduit)


class Publisher(PluginStep):
    description = _("Publishing Debian artifacts")

    def __init__(self, repo, conduit, config, plugin_type, **kwargs):
        super(Publisher, self).__init__(step_type=constants.PUBLISH_REPO_STEP,
                                        repo=repo,
                                        conduit=conduit,
                                        config=config,
                                        plugin_type=plugin_type)
        self.description = self.__class__.description
        self.add_child(ModulePublisher(conduit=conduit,
                                       config=config, repo=repo))
        repo_relative_path = configuration.get_repo_relative_path(repo, config)
        master_publish_dir = configuration.get_master_publish_dir(
            repo, plugin_type)
        target_directories = []
        listing_steps = []
        if config.get(constants.PUBLISH_HTTP_KEYWORD):
            root_publish_dir = configuration.get_http_publish_dir(config)
            repo_publish_dir = os.path.join(root_publish_dir,
                                            repo_relative_path)
            target_directories.append(('/', repo_publish_dir))
            listing_steps.append(GenerateListingFileStep(root_publish_dir,
                                                         repo_publish_dir))
        if config.get(constants.PUBLISH_HTTPS_KEYWORD):
            root_publish_dir = configuration.get_https_publish_dir(config)
            repo_publish_dir = os.path.join(root_publish_dir,
                                            repo_relative_path)
            target_directories.append(('/', repo_publish_dir))
            listing_steps.append(GenerateListingFileStep(root_publish_dir,
                                                         repo_publish_dir))
        atomic_publish_step = AtomicDirectoryPublishStep(
            self.get_working_dir(),
            target_directories,
            master_publish_dir)
        atomic_publish_step.description = _("Publishing files to web")
        self.add_child(atomic_publish_step)
        for step in listing_steps:
            self.add_child(step)


class ModulePublisher(PluginStep):
    description = _("Publishing modules")

    def __init__(self, **kwargs):
        kwargs.setdefault('step_type', constants.PUBLISH_MODULES_STEP)
        super(ModulePublisher, self).__init__(**kwargs)
        self.description = self.__class__.description

        self.publish_releases = PublishDebReleaseStep()
        self.add_child(self.publish_releases)

        self.publish_components = PublishDebComponentStep()
        self.add_child(self.publish_components)

        self.publish_units = PublishDebStep()
        self.add_child(self.publish_units)

        self.add_child(MetadataStep())

        if self.non_halting_exceptions is None:
            self.non_halting_exceptions = []

    def _get_total(self):
        return len(self.publish_units.unit_dict)


class PublishDebReleaseStep(UnitModelPluginStep):
    ID_PUBLISH_STEP = constants.PUBLISH_DEB_RELEASE_STEP
    Model = models.DebRelease

    def __init__(self, **kwargs):
        super(PublishDebReleaseStep, self).__init__(
            self.ID_PUBLISH_STEP, [self.Model], **kwargs)
        self.units = []

    def process_main(self, item=None):
        self.units.append(item)


class PublishDebComponentStep(UnitModelPluginStep):
    ID_PUBLISH_STEP = constants.PUBLISH_DEB_COMP_STEP
    Model = models.DebComponent

    def __init__(self, **kwargs):
        super(PublishDebComponentStep, self).__init__(
            self.ID_PUBLISH_STEP, [self.Model], **kwargs)
        self.units = []

    def process_main(self, item=None):
        self.units.append(item)


class PublishDebStep(UnitModelPluginStep):
    ID_PUBLISH_STEP = constants.PUBLISH_DEB_STEP
    Model = models.DebPackage

    def __init__(self, **kwargs):
        super(PublishDebStep, self).__init__(
            self.ID_PUBLISH_STEP, [self.Model], **kwargs)
        self.unit_dict = {}

    def process_main(self, item=None):
        self.unit_dict[item.id] = item


class MetadataStep(PluginStep):
    def __init__(self):
        super(MetadataStep, self).__init__(constants.PUBLISH_REPODATA)

    def process_main(self, item=None):
        unit_dict = self.parent.publish_units.unit_dict
        comp_units = self.parent.publish_components.units
        release_units = self.parent.publish_releases.units
        repo = self.get_repo()
        config = self.get_config()
        base_path = self.get_working_dir()

        # Add missing checksum fields (this is bad for performance):
        # This is a temporary fix!
        for package in unit_dict.itervalues():
            checksums = models.DebPackage.calculate_deb_checksums(package.storage_path)
            package.sha1 = checksums['sha1']
            package.sha256 = checksums['sha256']
            package.md5sum = checksums['md5sum']

        # Do nothing, if the repository is empty (review this behaviour):
        if len(unit_dict) == 0:
            return

        # If there are no release_units (old style repo) publish as 'stable/main':
        if len(release_units) == 0:
            default_release = models.DebRelease(suite='stable')
            release_units.append(default_release)
            all_component = models.DebComponent(
                name='main',
                release='stable',
                packages=[package_id for package_id in unit_dict],
            )
            comp_units.append(all_component)

        # If configured to do so, also publish as 'default/all':
        if config.get(constants.PUBLISH_DEFAULT_RELEASE_KEYWORD, False):
            default_release = models.DebRelease(codename='default', suite='default')
            release_units.append(default_release)
            all_component = models.DebComponent(
                name='all',
                release='default',
                packages=[package_id for package_id in unit_dict],
            )
            comp_units.append(all_component)

        # Create the 'pool' folder:
        pool_path = os.path.join(base_path, 'pool')
        os.mkdir(pool_path)

        # Symlink packages for each component in 'pool/<component_name>/':
        for component in comp_units:
            component_path = os.path.join(pool_path, component.name)
            if not os.path.exists(component_path):
                # Use makedirs() since component.name may contain '/'!
                os.makedirs(component_path)

            # Should we add additional subdirectories here (e.g.: /a/; /liba/)?
            for package_id in component.packages:
                package = unit_dict[package_id]
                destination_path = os.path.join(component_path, package.filename)
                if not os.path.exists(destination_path):
                    os.symlink(package.storage_path, destination_path)

        # Create the 'dists' folder:
        dists_path = os.path.join(base_path, 'dists')
        os.mkdir(dists_path)

        # Create the 'dists' folder structure for each release:
        for release in release_units:
            release_meta_data = {
                'architectures': set(),
                'components': set(),
            }
            release_meta_files = []
            release_name = None
            if release.codename:
                release_name = release.codename
                release_meta_data['codename'] = release.codename
            if release.suite:
                if not release_name:
                    release_name = release.suite
                release_meta_data['suite'] = release.suite
            if not release_name:
                raise RuntimeError('Neither codename nor suite is set!')
            release_path = os.path.join(dists_path, release_name)
            os.mkdir(release_path)

            # Continue the 'dists' folder structure for each component...
            for component in comp_units:
                # ...of the current release:
                if component.release == release_name:
                    release_meta_data['components'].add(component.name)
                    component_path = os.path.join(release_path, component.name)
                    # Use makedirs() since component.name may contain '/'!
                    os.makedirs(component_path)

                    # Create arch_units since there is no corresponding db entry:
                    # Note: This method will not create arches containing no arch
                    # specific packages (see: https://pulp.plan.io/issues/4094).
                    # (also not great for performance)
                    arch_units = defaultdict(list)
                    for package_id in component.packages:
                        package = unit_dict.get(package_id)
                        if package:
                            arch_units[package.architecture].append(package)

                    # The units/packages for arch all need to be appended to
                    # every other arch:
                    all_units = arch_units.pop('all', [])
                    for arch in arch_units:
                        arch_units[arch].extend(all_units)
                    # ...and then be readded to the list of architectures:
                    arch_units['all'] = all_units

                    # Now create 'binary-<arch>' folders for each arch:
                    for arch, packages in arch_units.items():
                        release_meta_data['architectures'].add(arch)
                        arch_folder = 'binary-' + arch
                        arch_path = os.path.join(component_path, arch_folder)
                        os.mkdir(arch_path)

                        # Create 'Packages' files for each arch:
                        packages_file_path = write_packages_file(arch_path,
                                                                 component.name,
                                                                 packages,)

                        # Compress and record 'Packages' files:
                        release_meta_files.append(packages_file_path)
                        gz_file_path = gzip_compress_file(packages_file_path)
                        release_meta_files.append(gz_file_path)
                        bz2_file_path = bz2_compress_file(packages_file_path)
                        release_meta_files.append(bz2_file_path)

            # Create the 'Release' file (for each release):
            release_meta_data['architectures'].remove('all')
            if repo.description:
                release_meta_data['description'] = repo.description
            release_meta_data['label'] = repo.id
            release_file_path = write_release_file(release_path,
                                                   release_meta_data,
                                                   release_meta_files,)

            signer = configuration.get_gpg_signer(repo, config)
            if signer is not None:
                signer.sign(release_file_path)


class GenerateListingFileStep(PluginStep):
    def __init__(self, root_dir, target_dir,
                 step=constants.PUBLISH_GENERATE_LISTING_FILE_STEP):
        """
        Initialize and set the ID of the step
        """
        super(GenerateListingFileStep, self).__init__(step)
        self.description = _("Writing Listings File")
        self.root_dir = root_dir
        self.target_dir = target_dir

    def process_main(self, item=None):
        yum_plugin_util.generate_listing_files(self.root_dir, self.target_dir)
