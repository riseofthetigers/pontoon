"""
Models for working with remote translation data stored in a VCS.
"""
import logging
import os.path
from itertools import chain

from django.utils.functional import cached_property

from pontoon.base import MOZILLA_REPOS
from pontoon.base.models import Resource
from pontoon.base.utils import extension_in, first


log = logging.getLogger(__name__)


def is_resource(filename):
    """
    Return True if the filename's extension is a supported Resource
    format.
    """
    return extension_in(filename, Resource.ALLOWED_EXTENSIONS)


def is_source_resource(filename):
    """
    Return True if the filename's extension is a source-only Resource
    format.
    """
    return extension_in(filename, Resource.SOURCE_EXTENSIONS)


def directory_contains_resources(directory_path, source_only=False):
    """
    Return True if the given directory contains at least one
    supported resource file (checked via file extension), or False
    otherwise.

    :param source_only:
        If True, only check for source-only formats.
    """
    resource_check = is_source_resource if source_only else is_resource
    for root, dirnames, filenames in os.walk(directory_path):
        # first() avoids checking past the first matching resouce.
        if first(filenames, resource_check) is not None:
            return True
    return False


class VCSProject(object):
    """
    Container for project data that is stored on the filesystem and
    pulled from a remote VCS.
    """
    SOURCE_DIR_SCORES = {
        'templates': 3,
        'en-US': 2,
        'en': 1
    }
    SOURCE_DIR_NAMES = SOURCE_DIR_SCORES.keys()

    def __init__(self, db_project):
        """
        Load resource paths from the given db_project and parse them
        for translation data.
        """
        self.db_project = db_project

    @cached_property
    def resources(self):
        """
        Lazy-loaded mapping of relative paths -> VCSResources.

        Waiting until first access both avoids unnecessary file reads
        and allows tests that don't need to touch the resources to run
        with less mocking.
        """
        # Avoid circular import; someday we should refactor to avoid.
        from pontoon.base.formats.base import ParseError

        resources = {}
        for path in self.relative_resource_paths():
            try:
                resources[path] = VCSResource(self, path)
            except ParseError as err:
                log.error('Skipping resource {path} due to ParseError: {err}'.format(
                    path=path, err=err
                ))
        return resources

    @property
    def entities(self):
        return chain.from_iterable(
            resource.entities.values() for resource in self.resources.values()
        )

    @property
    def checkout_path(self):
        return self.db_project.checkout_path

    def source_directory_path(self):
        """
        Path to the directory where source strings are stored.

        Paths are identified using a scoring system; more likely
        directory names get higher scores, as do directories with
        formats that only used for source strings.
        """
        possible_sources = []
        for root, dirnames, filenames in os.walk(self.checkout_path):
            for dirname in dirnames:
                if dirname in self.SOURCE_DIR_NAMES:
                    score = self.SOURCE_DIR_SCORES[dirname]

                    # Ensure the matched directory contains resources.
                    directory_path = os.path.join(root, dirname)
                    if directory_contains_resources(directory_path):
                        # Extra points for source resources!
                        if directory_contains_resources(directory_path, source_only=True):
                            score += 3

                        possible_sources.append((directory_path, score))

        if possible_sources:
            return max(possible_sources, key=lambda s: s[1])[0]
        else:
            raise Exception('No source directory found for project {0}'
                            .format(self.db_project.slug))

    def locale_directory_path(self, locale_code):
        """
        Path to the directory where strings for the given locale are
        stored.
        """
        path = self.checkout_path
        for root, dirnames, filenames in os.walk(path):
            if locale_code in dirnames:
                return os.path.join(root, locale_code)

            locale_variant = locale_code.replace('-', '_')
            if locale_variant in dirnames:
                return os.path.join(root, locale_variant)

        raise Exception('Directory for locale `{0}` not found'.format(
                        locale_code or 'source'))

    def relative_resource_paths(self):
        """
        List of paths relative to the locale directories returned by
        self.locale_directory_path() for each resource in this project.
        """
        path = self.source_directory_path()
        for absolute_path in self.resources_for_path(path):
            # .pot files in the source directory need to be renamed to
            # .po files for the locale directories.
            if absolute_path.endswith('.pot'):
                absolute_path = absolute_path[:-1]

            yield os.path.relpath(absolute_path, path)

    def resources_for_path(self, path):
        """
        List of paths for all supported resources found within the given
        path.
        """
        for root, dirnames, filenames in os.walk(path):
            # Ignore certain files in Mozilla repositories.
            if self.db_project.repository_url in MOZILLA_REPOS:
                filenames = [f for f in filenames if not f.endswith('region.properties')]

            for filename in filenames:
                if is_resource(filename):
                    yield os.path.join(root, filename)


class VCSResource(object):
    """Represents a single resource across multiple locales."""

    def __init__(self, vcs_project, path):
        """
        Load the resource file for each enabled locale and store its
        translations in VCSEntity instances.
        """
        from pontoon.base import formats  # Avoid circular import.

        self.vcs_project = vcs_project
        self.path = path
        self.files = {}
        self.entities = {}

        # Create entities using resources from the source directory,
        source_resource_path = os.path.join(vcs_project.source_directory_path(), self.path)

        # Special case: Source files for pofiles are actually .pot.
        if source_resource_path.endswith('po'):
            source_resource_path += 't'

        source_resource_file = formats.parse(source_resource_path)
        for index, translation in enumerate(source_resource_file.translations):
            vcs_entity = VCSEntity(
                resource=self,
                key=translation.key,
                string=translation.source_string,
                string_plural=translation.source_string_plural,
                comments=translation.comments,
                source=translation.source,
                order=translation.order or index
            )
            self.entities[vcs_entity.key] = vcs_entity

        # Fill in translations from the locale resources.
        for locale in vcs_project.db_project.locales.all():
            resource_path = os.path.join(
                vcs_project.locale_directory_path(locale.code),
                self.path
            )
            try:
                resource_file = formats.parse(resource_path, source_resource_path)
            except IOError:
                continue  # File doesn't exist, let's move on

            self.files[locale] = resource_file
            for translation in resource_file.translations:
                try:
                    self.entities[translation.key].translations[locale.code] = translation
                except KeyError:
                    # If the source is missing an entity, we consider it
                    # deleted and don't add it.
                    pass

    def save(self):
        """
        Save changes made to any of the translations in this resource
        back to the filesystem for all locales.
        """
        for locale, resource_file in self.files.items():
            resource_file.save(locale)


class VCSEntity(object):
    """
    An Entity is a single string to be translated, and a VCSEntity
    stores the translations for an entity from several locales.
    """
    def __init__(self, resource, key, string, comments, source, string_plural='',
                 order=0):
        self.resource = resource
        self.key = key
        self.string = string
        self.string_plural = string_plural
        self.translations = {}
        self.comments = comments
        self.source = source
        self.order = order

    def has_translation_for(self, locale_code):
        """Return True if a translation exists for the given locale."""
        return locale_code in self.translations


class VCSTranslation(object):
    """
    A single translation of a source string into another language.

    Since a string can have different translations based on plural
    forms, all of the different forms are stored under self.strings, a
    dict where the keys equal possible values for
    pontoon.base.models.Translation.plural_form and the values equal the
    translation for that plural form.
    """
    def __init__(
        self, key, strings, comments, fuzzy,
        source_string='',
        source_string_plural='',
        order=0,
        source=None,
        last_translator=None,
        last_updated=None
    ):
        self.key = key
        self.source_string = source_string
        self.source_string_plural = source_string_plural
        self.strings = strings
        self.comments = comments
        self.fuzzy = fuzzy
        self.order = order
        self.source = source or []
        self.last_translator = last_translator
        self.last_updated = last_updated

    @property
    def extra(self):
        """
        Return a dict of custom properties to store in the database.
        Useful for subclasses from specific formats that have extra data
        that needs to be preserved.
        """
        return {}
