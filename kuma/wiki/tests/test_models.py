import json
import time
from cStringIO import StringIO
from datetime import date, datetime, timedelta
from xml.sax.saxutils import escape

import mock
import pytest
from constance import config
from constance.test import override_config
from waffle.models import Switch

from django.conf import settings
from django.core.exceptions import ValidationError

from kuma.core.exceptions import ProgrammingError
from kuma.core.tests import KumaTestCase, eq_, get_user, ok_
from kuma.attachments.models import Attachment, AttachmentRevision
from kuma.users.tests import UserTestCase

from . import (create_document_tree, create_template_test_users,
               create_topical_parents_docs, document, normalize_html,
               revision)
from .. import tasks
from ..constants import REDIRECT_CONTENT, TEMPLATE_TITLE_PREFIX
from ..events import EditDocumentInTreeEvent
from ..exceptions import (DocumentRenderedContentNotAvailable,
                          DocumentRenderingInProgress, PageMoveError)
from ..models import Document, Revision, RevisionIP, TaggedDocument
from ..templatetags.jinja_helpers import absolutify
from ..utils import tidy_content
from ..signals import render_done


def _objects_eq(manager, list_):
    """Assert that the objects contained by `manager` are those in `list_`."""
    eq_(set(manager.all()), set(list_))


def redirect_rev(title, redirect_to):
    return revision(
        document=document(title=title, save=True),
        content='REDIRECT [[%s]]' % redirect_to,
        is_approved=True,
        save=True)


class DocumentTests(UserTestCase):
    """Tests for the Document model"""

    def test_json_data(self):
        """bug 875349"""
        # Set up a doc with tags
        rev = revision(is_approved=True, save=True, content='Sample document')
        doc = rev.document
        expected_tags = sorted(['foo', 'bar', 'baz'])
        expected_review_tags = sorted(['tech', 'editorial'])
        doc.tags.set(*expected_tags)
        doc.current_revision.review_tags.set(*expected_review_tags)

        # Create a translation with some tags
        de_doc = document(parent=doc, locale='de', save=True)
        revision(document=de_doc, save=True)
        expected_l10n_tags = ['inprogress']
        de_doc.current_revision.localization_tags.set(*expected_l10n_tags)
        de_doc.tags.set(*expected_tags)
        de_doc.current_revision.review_tags.set(*expected_review_tags)

        # Ensure the doc's json field is empty at first
        assert doc.json is None

        # Get JSON data for the doc, and ensure the doc's json field is now
        # properly populated.
        data = doc.get_json_data()
        assert doc.json == json.dumps(data)

        # Load up another copy of the doc from the DB, and check json
        saved_doc = Document.objects.get(pk=doc.pk)
        assert saved_doc.json == json.dumps(data)

        # Check the fields stored in JSON of the English doc
        # (the fields are created in build_json_data in models.py)
        assert data['title'] == doc.title
        assert data['label'] == doc.title
        assert data['url'] == doc.get_absolute_url()
        assert data['id'] == doc.id
        assert data['uuid'] == str(doc.uuid)
        assert data['slug'] == doc.slug
        result_tags = sorted([str(x) for x in data['tags']])
        assert result_tags == expected_tags
        result_review_tags = sorted([str(x) for x in data['review_tags']])
        assert result_review_tags == expected_review_tags
        assert data['locale'] == doc.locale
        assert data['summary'] == doc.current_revision.summary
        assert data['modified'] == doc.modified.isoformat()
        assert data['last_edit'] == doc.current_revision.created.isoformat()

        # Check fields of translated doc
        assert 'translations' in data
        assert len(data['translations']) == 1
        de_data = data['translations'][0]
        assert de_data['locale'] == de_doc.locale
        result_l10n_tags = sorted([str(x) for x
                                   in de_data['localization_tags']])
        assert result_l10n_tags == expected_l10n_tags
        result_tags = sorted([str(x) for x in de_data['tags']])
        assert result_tags == expected_tags
        result_review_tags = sorted([str(x) for x in de_data['review_tags']])
        assert result_review_tags == expected_review_tags
        assert de_data['summary'] == de_doc.current_revision.summary
        assert de_data['title'] == de_doc.title
        assert de_data['uuid'] == str(de_doc.uuid)

    def test_json_data_null_uuid(self):
        """Test json data during the UUID transition period."""
        rev = revision(is_approved=True, save=True, content='Sample document')
        doc = rev.document
        doc.uuid = None
        doc.save()
        de_doc = document(parent=doc, locale='de', uuid=None, save=True)
        revision(document=de_doc, save=True)

        data = doc.get_json_data()
        assert data['uuid'] is None
        assert 'translations' in data
        assert len(data['translations']) == 1
        assert data['translations'][0]['uuid'] is None

    def test_document_is_template(self):
        """is_template stays in sync with the title"""
        d = document(title='test')
        d.save()

        assert not d.is_template

        d.slug = '%stest' % TEMPLATE_TITLE_PREFIX
        d.save()

        assert d.is_template

        d.slug = 'Back-to-document'
        d.save()

        assert not d.is_template

    def test_error_on_delete(self):
        """Ensure error-on-delete is only thrown when waffle switch active"""
        switch = Switch.objects.create(name='wiki_error_on_delete')

        for active in (True, False):
            switch.active = active
            switch.save()

            d = document()
            d.save()

            try:
                d.delete()
                if active:
                    self.fail('Exception on delete when active')
            except Exception:
                if not active:
                    self.fail('No exception on delete when not active')

    def test_delete_tagged_document(self):
        """Make sure deleting a tagged doc deletes its tag relationships."""
        # TODO: Move to wherever the tests for TaggableMixin are.
        # This works because Django's delete() sees the `tags` many-to-many
        # field (actually a manager) and follows the reference.
        d = document()
        d.save()
        d.tags.add('grape')
        eq_(1, TaggedDocument.objects.count())

        d.delete()
        eq_(0, TaggedDocument.objects.count())

    def test_only_localizable_allowed_children(self):
        """You can't have children for a non-localizable document."""
        # Make English rev:
        en_doc = document(is_localizable=False)
        en_doc.save()

        # Make Deutsch translation:
        de_doc = document(parent=en_doc, locale='de')
        self.assertRaises(ValidationError, de_doc.save)

    def test_cannot_make_non_localizable_if_children(self):
        """You can't make a document non-localizable if it has children."""
        # Make English rev:
        en_doc = document(is_localizable=True)
        en_doc.save()

        # Make Deutsch translation:
        de_doc = document(parent=en_doc, locale='de')
        de_doc.save()
        en_doc.is_localizable = False
        self.assertRaises(ValidationError, en_doc.save)

    def test_non_english_implies_nonlocalizable(self):
        d = document(is_localizable=True, locale='de')
        d.save()
        assert not d.is_localizable

    def test_other_translations(self):
        """
        parent doc should list all docs for which it is parent

        A child doc should list all its parent's docs, excluding itself, and
        including its parent
        """
        parent = document(locale=settings.WIKI_DEFAULT_LANGUAGE, title='test',
                          save=True)
        enfant = document(locale='fr', title='le test', parent=parent,
                          save=True)
        bambino = document(locale='es', title='el test', parent=parent,
                           save=True)

        children = (Document.objects.filter(parent=parent)
                                    .order_by('locale')
                                    .values_list('pk', flat=True))
        eq_(list(children),
            list(parent.other_translations.values_list('pk', flat=True)))

        enfant_translation_pks = (enfant.other_translations
                                        .values_list('pk', flat=True))
        ok_(parent.pk in enfant_translation_pks)
        ok_(bambino.pk in enfant_translation_pks)
        eq_(False, enfant.pk in enfant_translation_pks)

    def test_topical_parents(self):
        d1, d2 = create_topical_parents_docs()
        ok_(d2.parents == [d1])

        d3 = document(title='Smell accessibility')
        d3.parent_topic = d2
        d3.save()
        ok_(d3.parents == [d1, d2])

    @pytest.mark.redirect
    def test_redirect_url_allows_site_url(self):
        href = "%s/en-US/Mozilla" % settings.SITE_URL
        title = "Mozilla"
        html = REDIRECT_CONTENT % {'href': href, 'title': title}
        d = document(is_redirect=True, html=html)
        eq_(href, d.get_redirect_url())

    @pytest.mark.redirect
    def test_redirect_url_allows_domain_relative_url(self):
        href = "/en-US/Mozilla"
        title = "Mozilla"
        html = REDIRECT_CONTENT % {'href': href, 'title': title}
        d = document(is_redirect=True, html=html)
        eq_(href, d.get_redirect_url())

    @pytest.mark.redirect
    def test_redirect_url_rejects_protocol_relative_url(self):
        href = "//evilsite.com"
        title = "Mozilla"
        html = REDIRECT_CONTENT % {'href': href, 'title': title}
        d = document(is_redirect=True, html=html)
        eq_(None, d.get_redirect_url())

    @pytest.mark.redirect
    def test_redirect_url_works_for_home_path(self):
        """bug 1082034"""
        href = "/"
        title = "Mozilla"
        html = REDIRECT_CONTENT % {'href': href, 'title': title}
        d = document(is_redirect=True, html=html)
        eq_(href, d.get_redirect_url())

    def test_get_full_url(self):
        doc = document()
        eq_(doc.get_full_url(), absolutify(doc.get_absolute_url()))


class PermissionTests(KumaTestCase):

    def setUp(self):
        """Set up the permissions, groups, and users needed for the tests"""
        super(PermissionTests, self).setUp()
        (self.perms, self.groups, self.users, self.superuser) = (
            create_template_test_users())

    def test_template_permissions(self):
        msg = ('should not', 'should')

        for is_add in (True, False):

            slug_trials = (
                ('test_for_%s', (
                    (True, self.superuser),
                    (True, self.users['none']),
                    (True, self.users['all']),
                    (True, self.users['add']),
                    (True, self.users['change']),
                )),
                ('Template:test_for_%s', (
                    (True, self.superuser),
                    (False, self.users['none']),
                    (True, self.users['all']),
                    (is_add, self.users['add']),
                    (not is_add, self.users['change']),
                ))
            )

            for slug_tmpl, trials in slug_trials:
                for expected, trial_user in trials:
                    slug = slug_tmpl % trial_user.username
                    if is_add:
                        eq_(expected,
                            Document.objects.allows_add_by(trial_user, slug),
                            'User %s %s able to create %s' % (
                                trial_user, msg[expected], slug))
                    else:
                        doc = document(slug=slug, title=slug)
                        eq_(expected,
                            doc.allows_revision_by(trial_user),
                            'User %s %s able to revise %s' % (
                                trial_user, msg[expected], slug))
                        eq_(expected,
                            doc.allows_editing_by(trial_user),
                            'User %s %s able to edit %s' % (
                                trial_user, msg[expected], slug))


class UserDocumentTests(UserTestCase):
    """Document tests which need the users fixture"""

    def test_default_topic_parents_for_translation(self):
        """A translated document with no topic parent should by default use
        the translation of its translation parent's topic parent."""
        orig_pt = document(locale=settings.WIKI_DEFAULT_LANGUAGE,
                           title='test section',
                           save=True)
        orig = document(locale=settings.WIKI_DEFAULT_LANGUAGE, title='test',
                        parent_topic=orig_pt, save=True)

        trans_pt = document(locale='fr', title='le test section',
                            parent=orig_pt, save=True)
        trans = document(locale='fr', title='le test',
                         parent=orig, save=True)

        ok_(trans.parent_topic)
        eq_(trans.parent_topic.pk, trans_pt.pk)

    def test_default_topic_with_stub_creation(self):
        orig_pt = document(locale=settings.WIKI_DEFAULT_LANGUAGE,
                           title='test section',
                           save=True)
        orig = document(locale=settings.WIKI_DEFAULT_LANGUAGE, title='test',
                        parent_topic=orig_pt, save=True)

        trans = document(locale='fr', title='le test',
                         parent=orig, save=True)

        # There should be a translation topic parent
        trans_pt = trans.parent_topic
        ok_(trans_pt)
        # The locale of the topic parent should match the new translation
        eq_(trans.locale, trans_pt.locale)
        # But, the translation's topic parent must *not* be the translation
        # parent's topic parent
        ok_(trans_pt.pk != orig_pt.pk)
        # Still, since the topic parent is an autocreated stub, it shares its
        # title with the original.
        eq_(trans_pt.title, orig_pt.title)
        # Oh, and it should point to the original parent topic as its
        # translation parent
        eq_(trans_pt.parent, orig_pt)

    def test_default_topic_with_path_gaps(self):
        # Build a path of docs in en-US
        orig_path = ('MDN', 'web', 'CSS', 'properties', 'banana', 'leaf')
        docs, doc = [], None
        for title in orig_path:
            doc = document(locale=settings.WIKI_DEFAULT_LANGUAGE, title=title,
                           parent_topic=doc, save=True)
            revision(document=doc, title=title, save=True)
            docs.append(doc)

        # Translate, but leave gaps for stubs
        trans_0 = document(locale='fr', title='le MDN',
                           parent=docs[0], save=True)
        revision(document=trans_0, title='le MDN', tags="LeTest!", save=True)
        trans_2 = document(locale='fr', title='le CSS',
                           parent=docs[2], save=True)
        revision(document=trans_2, title='le CSS', tags="LeTest!", save=True)
        trans_5 = document(locale='fr', title='le leaf',
                           parent=docs[5], save=True)
        revision(document=trans_5, title='le ;eaf', tags="LeTest!", save=True)

        # Make sure trans_2 got the right parent
        eq_(trans_2.parents[0].pk, trans_0.pk)

        # Ensure the translated parents and stubs appear properly in the path
        parents_5 = trans_5.parents
        eq_(parents_5[0].pk, trans_0.pk)
        eq_(parents_5[1].locale, trans_5.locale)
        eq_(parents_5[1].title, docs[1].title)
        ok_(parents_5[1].current_revision.pk != docs[1].current_revision.pk)
        eq_(parents_5[2].pk, trans_2.pk)
        eq_(parents_5[3].locale, trans_5.locale)
        eq_(parents_5[3].title, docs[3].title)
        ok_(parents_5[3].current_revision.pk != docs[3].current_revision.pk)
        eq_(parents_5[4].locale, trans_5.locale)
        eq_(parents_5[4].title, docs[4].title)
        ok_(parents_5[4].current_revision.pk != docs[4].current_revision.pk)

        for p in parents_5:
            ok_(p.current_revision)
            if p.pk not in (trans_0.pk, trans_2.pk, trans_5.pk):
                ok_('NeedsTranslation' in p.current_revision.tags)
                ok_('TopicStub' in p.current_revision.tags)
                ok_(p.current_revision.localization_in_progress)

    def test_repair_breadcrumbs(self):
        english_top = document(locale=settings.WIKI_DEFAULT_LANGUAGE,
                               title='English top',
                               save=True)
        english_mid = document(locale=settings.WIKI_DEFAULT_LANGUAGE,
                               title='English mid',
                               parent_topic=english_top,
                               save=True)
        english_bottom = document(locale=settings.WIKI_DEFAULT_LANGUAGE,
                                  title='English bottom',
                                  parent_topic=english_mid,
                                  save=True)

        french_top = document(locale='fr',
                              title='French top',
                              parent=english_top,
                              save=True)
        french_mid = document(locale='fr',
                              parent=english_mid,
                              parent_topic=english_mid,
                              save=True)
        french_bottom = document(locale='fr',
                                 parent=english_bottom,
                                 parent_topic=english_bottom,
                                 save=True)

        french_bottom.repair_breadcrumbs()
        french_bottom_fixed = Document.objects.get(locale='fr',
                                                   title=french_bottom.title)
        eq_(french_mid.id, french_bottom_fixed.parent_topic.id)
        eq_(french_top.id, french_bottom_fixed.parent_topic.parent_topic.id)

    def test_code_sample_extraction(self):
        """Make sure sample extraction works from the model.
        This is a smaller version of the test from test_content.py"""
        sample_html = u'<p class="foo">Hello world!</p>'
        sample_css = u'.foo p { color: red; }'
        sample_js = u'window.alert("Hi there!");'
        doc_src = u"""
            <p>This is a page. Deal with it.</p>
            <ul id="s2" class="code-sample">
                <li><pre class="brush: html">%s</pre></li>
                <li><pre class="brush: css">%s</pre></li>
                <li><pre class="brush: js">%s</pre></li>
            </ul>
            <p>More content shows up here.</p>
        """ % (escape(sample_html), escape(sample_css), escape(sample_js))

        rev = revision(is_approved=True, save=True, content=doc_src)
        result = rev.document.extract.code_sample('s2')
        eq_(sample_html.strip(), result['html'].strip())
        eq_(sample_css.strip(), result['css'].strip())
        eq_(sample_js.strip(), result['js'].strip())

    def test_tree_is_watched_by(self):
        rev = revision()
        testuser2 = get_user(username='testuser2')
        EditDocumentInTreeEvent.notify(testuser2, rev.document)

        assert rev.document.tree_is_watched_by(testuser2)

    def test_parent_trees_watched_by(self):
        root_doc, child_doc, grandchild_doc = create_document_tree()
        testuser2 = get_user(username='testuser2')

        EditDocumentInTreeEvent.notify(testuser2, root_doc)
        EditDocumentInTreeEvent.notify(testuser2, child_doc)

        assert 2 == len(grandchild_doc.parent_trees_watched_by(testuser2))


class TaggedDocumentTests(UserTestCase):
    """Tests for tags in Documents and Revisions"""

    @pytest.mark.tags
    def test_revision_tags(self):
        """Change tags on Document by creating Revisions"""
        rev = revision(is_approved=True, save=True, content='Sample document')

        eq_(0, Document.objects.filter(tags__name='foo').count())
        eq_(0, Document.objects.filter(tags__name='alpha').count())

        r = revision(document=rev.document, content='Update to document',
                     is_approved=True, tags="foo, bar, baz")
        r.save()

        eq_(1, Document.objects.filter(tags__name='foo').count())
        eq_(0, Document.objects.filter(tags__name='alpha').count())

        r = revision(document=rev.document, content='Another update',
                     is_approved=True, tags="alpha, beta, gamma")
        r.save()

        eq_(0, Document.objects.filter(tags__name='foo').count())
        eq_(1, Document.objects.filter(tags__name='alpha').count())


class RevisionTests(UserTestCase):
    """Tests for the Revision model"""

    def test_approved_revision_updates_html(self):
        """Creating an approved revision updates document.html"""
        rev = revision(is_approved=True, save=True,
                       content='Replace document html')

        assert 'Replace document html' in rev.document.html, \
               '"Replace document html" not in %s' % rev.document.html

        # Creating another approved revision replaces it again
        r = revision(document=rev.document, content='Replace html again',
                     is_approved=True)
        r.save()

        assert 'Replace html again' in rev.document.html, \
               '"Replace html again" not in %s' % rev.document.html

    def test_unapproved_revision_not_updates_html(self):
        """Creating an unapproved revision does not update document.html"""
        rev = revision(is_approved=True, save=True, content='Here to stay')

        assert 'Here to stay' in rev.document.html, \
               '"Here to stay" not in %s' % rev.document.html

        # Creating another approved revision keeps initial content
        r = revision(document=rev.document, content='Fail to replace html',
                     is_approved=False)
        r.save()

        assert 'Here to stay' in rev.document.html, \
               '"Here to stay" not in %s' % rev.document.html

    def test_revision_unicode(self):
        """Revision containing unicode characters is saved successfully."""
        content = u'Firefox informa\xe7\xf5es \u30d8\u30eb'
        rev = revision(is_approved=True, save=True, content=content)
        eq_(content, rev.content)

    def test_save_bad_based_on(self):
        """Saving a Revision with a bad based_on value raises an error."""
        r = revision()
        r.based_on = revision()  # Revision of some other unrelated Document
        self.assertRaises(ProgrammingError, r.save)

    def test_correct_based_on_to_none(self):
        """Assure Revision.clean() changes a bad based_on value to None when
        there is no current_revision of the English document."""
        r = revision()
        r.based_on = revision()  # Revision of some other unrelated Document
        self.assertRaises(ValidationError, r.clean)
        eq_(None, r.based_on)

    def test_correct_based_on_to_current_revision(self):
        """Assure Revision.clean() defaults based_on value to the English
        doc's current_revision when there is one."""
        # Make English rev:
        en_rev = revision(is_approved=True)
        en_rev.save()

        # Make Deutsch translation:
        de_doc = document(parent=en_rev.document, locale='de')
        de_doc.save()
        de_rev = revision(document=de_doc)

        # Set based_on to a de rev to simulate fixing broken translation source
        de_rev.based_on = de_rev
        de_rev.clean()
        eq_(en_rev.document.current_revision, de_rev.based_on)

    def test_previous(self):
        """Revision.previous should return this revision's document's
        most recent approved revision."""
        rev = revision(is_approved=True, save=True)
        eq_(None, rev.previous)
        # wait a second so next revision is a different datetime
        time.sleep(1)
        next_rev = revision(document=rev.document, content="Updated",
                            is_approved=True)
        next_rev.save()
        eq_(rev, next_rev.previous)
        time.sleep(1)
        last_rev = revision(document=rev.document, content="Finally",
                            is_approved=True)
        last_rev.save()
        eq_(next_rev, last_rev.previous)

    @pytest.mark.toc
    def test_show_toc(self):
        """Setting toc_depth appropriately affects the Document's
        show_toc property."""
        rev = revision(is_approved=True, save=True,
                       content='Toggle table of contents.')
        assert (rev.toc_depth != 0)
        assert rev.document.show_toc

        r = revision(document=rev.document, content=rev.content, toc_depth=0,
                     is_approved=True)
        r.save()
        assert not rev.document.show_toc

        r = revision(document=rev.document, content=r.content, toc_depth=1,
                     is_approved=True)
        r.save()
        assert rev.document.show_toc

    def test_revert(self):
        """Reverting to a specific revision."""
        rev = revision(is_approved=True, save=True, content='Test reverting')
        old_id = rev.id

        revision(document=rev.document,
                 title='Test reverting',
                 content='An edit to revert',
                 comment='This edit gets reverted',
                 is_approved=True)
        rev.save()

        reverted = rev.document.revert(rev, rev.creator)
        ok_('Revert to' in reverted.comment)
        ok_('Test reverting' == reverted.content)
        ok_(old_id != reverted.id)

    def test_revert_review_tags(self):
        rev = revision(is_approved=True, save=True,
                       content='Test reverting with review tags')
        rev.review_tags.set('technical')

        r2 = revision(document=rev.document,
                      title='Test reverting with review tags',
                      content='An edit to revert',
                      comment='This edit gets reverted',
                      is_approved=True)
        r2.save()
        r2.review_tags.set('editorial')

        reverted = rev.document.revert(rev, rev.creator)
        reverted_tags = [t.name for t in reverted.review_tags.all()]
        ok_('technical' in reverted_tags)
        ok_('editorial' not in reverted_tags)

    def test_get_tidied_content_uses_model_field_first(self):
        content = '<h1>  Test get_tidied_content.  </h1>'
        fake_tidied = '<h1>  Fake tidied.  </h1>'
        rev = revision(is_approved=True, save=True, content=content,
                       tidied_content=fake_tidied)
        eq_(fake_tidied, rev.get_tidied_content())

    def test_get_tidied_content_tidies_in_process_by_default(self):
        content = '<h1>  Test get_tidied_content.  </h1>'
        rev = revision(is_approved=True, save=True, content=content)
        tidied_content, errors = tidy_content(content)
        eq_(tidied_content, rev.get_tidied_content())

    def test_get_tidied_content_returns_none_on_allow_none(self):
        rev = revision(is_approved=True, save=True,
                       content='Test get_tidied_content can return None.')
        eq_(None, rev.get_tidied_content(allow_none=True))


class GetCurrentOrLatestRevisionTests(UserTestCase):

    """Tests for current_or_latest_revision."""
    def test_single_approved(self):
        """Get approved revision."""
        rev = revision(is_approved=True, save=True)
        eq_(rev, rev.document.current_or_latest_revision())

    def test_multiple_approved(self):
        """When multiple approved revisions exist, return the most recent."""
        r1 = revision(is_approved=True, save=True)
        r2 = revision(is_approved=True, save=True, document=r1.document)
        eq_(r2, r2.document.current_or_latest_revision())

    def test_latest(self):
        """Return latest revision when no current exists."""
        r1 = revision(is_approved=False, save=True,
                      created=datetime.now() - timedelta(days=1))
        r2 = revision(is_approved=False, save=True, document=r1.document)
        eq_(r2, r1.document.current_or_latest_revision())


class DumpAndLoadJsonTests(UserTestCase):

    def test_roundtrip(self):
        # Create some documents and revisions here, rather than use a fixture
        r1 = revision(is_approved=True, save=True, content='Doc 1')
        r2 = revision(is_approved=True, save=True, content='Doc 2')
        r3 = revision(is_approved=True, save=True, content='Doc 3')
        r4 = revision(is_approved=True, save=True, content='Doc 4')
        r5 = revision(is_approved=True, save=True, content='Doc 5')

        # Since this happens in dev sometimes, break a doc by deleting its
        # current revision and leaving it with none.
        r5.document.current_revision = None
        r5.document.save()
        r5.delete()

        # The same creator will be used for all the revs, so let's also get a
        # non-creator user for the upload.
        creator = r1.creator
        uploader = self.user_model.objects.exclude(pk=creator.id).all()[0]

        # Count docs (with revisions) and revisions in DB
        doc_cnt_db = (Document.objects
                      .filter(current_revision__isnull=False)
                      .count())
        rev_cnt_db = (Revision.objects.count())

        # Do the dump, capture it, parse the JSON
        fin = StringIO()
        Document.objects.dump_json(Document.objects.all(), fin)
        data_json = fin.getvalue()
        data = json.loads(data_json)

        # No objects should come with non-null primary keys
        for x in data:
            ok_(not x['pk'])

        # Count the documents in JSON vs the DB
        doc_cnt_json = len([x for x in data if x['model'] == 'wiki.document'])
        eq_(doc_cnt_db, doc_cnt_json,
            "DB and JSON document counts should match")

        # Count the revisions in JSON vs the DB
        rev_cnt_json = len([x for x in data if x['model'] == 'wiki.revision'])
        eq_(rev_cnt_db, rev_cnt_json,
            "DB and JSON revision counts should match")

        # For good measure, ensure no documents missing revisions in the dump.
        doc_no_rev = (Document.objects
                      .filter(current_revision__isnull=True))[0]
        no_rev_cnt = len([x for x in data
                          if x['model'] == 'wiki.document' and
                          x['fields']['slug'] == doc_no_rev.slug and
                          x['fields']['locale'] == doc_no_rev.locale])
        eq_(0, no_rev_cnt,
            "There should be no document exported without revision")

        # Upload the data as JSON, assert that all objects were loaded
        loaded_cnt = Document.objects.load_json(uploader, StringIO(data_json))
        eq_(len(data), loaded_cnt)

        # Ensure the current revisions of the documents have changed, and that
        # the creator matches the uploader.
        for d_orig in (r1.document, r2.document, r3.document, r4.document):
            d_curr = Document.objects.get(pk=d_orig.pk)
            eq_(2, d_curr.revisions.count())
            ok_(d_orig.current_revision.id != d_curr.current_revision.id)
            ok_(d_orig.current_revision.creator_id !=
                d_curr.current_revision.creator_id)
            eq_(uploader.id, d_curr.current_revision.creator_id)

        # Everyone out of the pool!
        Document.objects.all().delete()
        Revision.objects.all().delete()

        # Try reloading the data on an empty DB
        loaded_cnt = Document.objects.load_json(uploader, StringIO(data_json))
        eq_(len(data), loaded_cnt)

        # Count docs (with revisions) and revisions in DB. The imported objects
        # should have beeen doc/rev pairs.
        eq_(loaded_cnt / 2, Document.objects.count())
        eq_(loaded_cnt / 2, Revision.objects.count())

        # The originals should be gone, now.
        for d_orig in (r1.document, r2.document, r3.document, r4.document):

            # The original primary key should have gone away.
            try:
                d_curr = Document.objects.get(pk=d_orig.pk)
                self.fail("This should have been an error")
            except Document.DoesNotExist:
                pass

            # Should be able to fetch document with the original natural key
            key = d_orig.natural_key()
            d_curr = Document.objects.get_by_natural_key(*key)
            eq_(1, d_curr.revisions.count())
            eq_(uploader.id, d_curr.current_revision.creator_id)


@override_config(
    KUMA_DOCUMENT_RENDER_TIMEOUT=600.0,
    KUMA_DOCUMENT_FORCE_DEFERRED_TIMEOUT=7.0)
class DeferredRenderingTests(UserTestCase):

    def setUp(self):
        super(DeferredRenderingTests, self).setUp()
        self.rendered_content = 'THIS IS RENDERED'
        self.raw_content = 'THIS IS NOT RENDERED CONTENT'
        self.r1 = revision(is_approved=True, save=True, content='Doc 1')
        self.d1 = self.r1.document
        config.KUMA_DOCUMENT_RENDER_TIMEOUT = 600.0
        config.KUMA_DOCUMENT_FORCE_DEFERRED_TIMEOUT = 7.0

    def tearDown(self):
        super(DeferredRenderingTests, self).tearDown()
        self.d1.delete()

    def test_rendering_fields(self):
        """Defaults for model fields related to rendering should work as
        expected"""
        ok_(not self.d1.rendered_html)
        ok_(not self.d1.defer_rendering)
        ok_(not self.d1.is_rendering_scheduled)
        ok_(not self.d1.is_rendering_in_progress)

    @override_config(KUMASCRIPT_TIMEOUT=1.0)
    @mock.patch('kuma.wiki.kumascript.get')
    def test_get_rendered(self, mock_kumascript_get):
        """get_rendered() should return rendered content when available,
        attempt a render() when it's not"""
        mock_kumascript_get.return_value = (self.rendered_content, None)

        # First, try getting the rendered version of a document. It should
        # trigger a call to kumascript.
        ok_(not self.d1.rendered_html)
        ok_(not self.d1.render_started_at)
        ok_(not self.d1.last_rendered_at)
        result_rendered, _ = self.d1.get_rendered(None, 'http://testserver/')
        ok_(mock_kumascript_get.called)
        eq_(self.rendered_content, result_rendered)
        eq_(self.rendered_content, self.d1.rendered_html)

        # Next, get a fresh copy of the document and try getting a rendering.
        # It should *not* call out to kumascript, because the rendered content
        # should be in the DB.
        d1_fresh = Document.objects.get(pk=self.d1.pk)
        eq_(self.rendered_content, d1_fresh.rendered_html)
        ok_(d1_fresh.render_started_at)
        ok_(d1_fresh.last_rendered_at)
        mock_kumascript_get.called = False
        result_rendered, _ = d1_fresh.get_rendered(None, 'http://testserver/')
        ok_(not mock_kumascript_get.called)
        eq_(self.rendered_content, result_rendered)

    @mock.patch('kuma.wiki.models.render_done')
    def test_build_json_on_render(self, mock_render_done):
        """
        A document's json field is refreshed on render(), but not on save()

        bug 875349
        """
        self.d1.save()
        ok_(not mock_render_done.send.called)
        mock_render_done.reset()

        self.d1.render()
        ok_(mock_render_done.send.called)

    @mock.patch('kuma.wiki.tasks.build_json_data_for_document')
    def test_render_signal(self, build_json_task):
        render_done.send(sender=Document, instance=self.d1)
        ok_(build_json_task.delay.called)

    @mock.patch('kuma.wiki.tasks.build_json_data_for_document')
    def test_render_signal_doc_deleted(self, build_json_task):
        self.d1.deleted = True
        render_done.send(sender=Document, instance=self.d1)
        ok_(not build_json_task.delay.called)

    @mock.patch('kuma.wiki.kumascript.get')
    @override_config(KUMASCRIPT_TIMEOUT=1.0)
    def test_get_summary(self, mock_kumascript_get):
        """
        get_summary() should attempt to use rendered
        """
        mock_kumascript_get.return_value = ('<p>summary!</p>', None)
        ok_(not self.d1.rendered_html)
        result_summary = self.d1.get_summary()
        ok_(not mock_kumascript_get.called)
        ok_(not self.d1.rendered_html)

        self.d1.render()
        ok_(self.d1.rendered_html)
        ok_(mock_kumascript_get.called)
        result_summary = self.d1.get_summary()
        eq_("summary!", result_summary)

    @mock.patch('kuma.wiki.kumascript.get')
    def test_one_render_at_a_time(self, mock_kumascript_get):
        """Only one in-progress rendering should be allowed for a Document"""
        mock_kumascript_get.return_value = (self.rendered_content, None)
        self.d1.render_started_at = datetime.now()
        self.d1.save()
        try:
            self.d1.render('', 'http://testserver/')
            self.fail("An attempt to render while another appears to be in "
                      "progress should be disallowed")
        except DocumentRenderingInProgress:
            pass

    @mock.patch('kuma.wiki.kumascript.get')
    @override_config(KUMA_DOCUMENT_RENDER_TIMEOUT=5.0)
    def test_render_timeout(self, mock_kumascript_get):
        """
        A rendering that has taken too long is no longer considered in progress
        """
        mock_kumascript_get.return_value = (self.rendered_content, None)
        self.d1.render_started_at = (datetime.now() -
                                     timedelta(seconds=5.0 + 1))
        self.d1.save()
        try:
            self.d1.render('', 'http://testserver/')
        except DocumentRenderingInProgress:
            self.fail("A timed-out rendering should not be considered as "
                      "still in progress")

    @mock.patch('kuma.wiki.kumascript.get')
    def test_long_render_sets_deferred(self, mock_kumascript_get):
        """A rendering that takes more than a desired response time marks the
        document as in need of deferred rendering in the future."""
        config.KUMASCRIPT_TIMEOUT = 1.0
        rendered_content = self.rendered_content

        def my_kumascript_get(self, cache_control, base_url, timeout):
            time.sleep(1.0)
            return (rendered_content, None)

        mock_kumascript_get.side_effect = my_kumascript_get

        config.KUMA_DOCUMENT_FORCE_DEFERRED_TIMEOUT = 2.0
        self.d1.render('', 'http://testserver/')
        ok_(not self.d1.defer_rendering)

        config.KUMA_DOCUMENT_FORCE_DEFERRED_TIMEOUT = 0.5
        self.d1.render('', 'http://testserver/')
        ok_(self.d1.defer_rendering)
        config.KUMASCRIPT_TIMEOUT = 0.0

    @mock.patch('kuma.wiki.kumascript.get')
    @mock.patch.object(tasks.render_document, 'delay')
    def test_schedule_rendering(self, mock_render_document_delay,
                                mock_kumascript_get):
        mock_kumascript_get.return_value = (self.rendered_content, None)
        # Scheduling for a non-deferred render should happen on the spot.
        self.d1.defer_rendering = False
        self.d1.save()
        ok_(not self.d1.render_scheduled_at)
        ok_(not self.d1.last_rendered_at)
        self.d1.schedule_rendering(None, 'http://testserver/')
        ok_(self.d1.render_scheduled_at)
        ok_(self.d1.last_rendered_at)
        ok_(not mock_render_document_delay.called)
        ok_(not self.d1.is_rendering_scheduled)

        # Reset the significant fields and try a deferred render.
        self.d1.last_rendered_at = None
        self.d1.render_started_at = None
        self.d1.render_scheduled_at = None
        self.d1.defer_rendering = True
        self.d1.save()

        # Scheduling for a deferred render should result in a queued task.
        self.d1.schedule_rendering(None, 'http://testserver/')
        ok_(self.d1.render_scheduled_at)
        ok_(not self.d1.last_rendered_at)
        ok_(mock_render_document_delay.called)

        # And, since our mock delay() doesn't actually queue a task, this
        # document should appear to be scheduled for a pending render not yet
        # in progress.
        ok_(self.d1.is_rendering_scheduled)
        ok_(not self.d1.is_rendering_in_progress)

    @mock.patch('kuma.wiki.kumascript.get')
    @mock.patch.object(tasks.render_document, 'delay')
    def test_deferred_vs_immediate_rendering(self, mock_render_document_delay,
                                             mock_kumascript_get):
        mock_kumascript_get.return_value = (self.rendered_content, None)

        switch = Switch.objects.create(name='wiki_force_immediate_rendering')

        # When defer_rendering == False, the rendering should be immediate.
        switch.active = False
        switch.save()
        self.d1.rendered_html = ''
        self.d1.defer_rendering = False
        self.d1.save()
        result_rendered, _ = self.d1.get_rendered(None, 'http://testserver/')
        ok_(not mock_render_document_delay.called)

        # When defer_rendering == True but the waffle switch forces immediate,
        # the rendering should be immediate.
        switch.active = True
        switch.save()
        self.d1.rendered_html = ''
        self.d1.defer_rendering = True
        self.d1.save()
        result_rendered, _ = self.d1.get_rendered(None, 'http://testserver/')
        ok_(not mock_render_document_delay.called)

        # When defer_rendering == True, the rendering should be deferred and an
        # exception raised if the content is blank.
        switch.active = False
        switch.save()
        self.d1.rendered_html = ''
        self.d1.defer_rendering = True
        self.d1.save()
        try:
            result_rendered, _ = self.d1.get_rendered(None, 'http://testserver/')
            self.fail("We should have gotten a "
                      "DocumentRenderedContentNotAvailable exception")
        except DocumentRenderedContentNotAvailable:
            pass
        ok_(mock_render_document_delay.called)

    @mock.patch('kuma.wiki.kumascript.get')
    def test_errors_stored_correctly(self, mock_kumascript_get):
        errors = [
            {'level': 'error', 'message': 'This is a fake error',
             'args': ['FakeError']},
        ]
        mock_kumascript_get.return_value = (self.rendered_content, errors)

        r_rendered, r_errors = self.d1.get_rendered(None, 'http://testserver/')
        ok_(errors, r_errors)


class RenderExpiresTests(UserTestCase):
    """Tests for max-age and automatic document rebuild"""

    def test_find_stale_documents(self):
        now = datetime.now()

        # Fresh
        d1 = document(title='Aged 1')
        d1.render_expires = now + timedelta(seconds=100)
        d1.save()

        # Stale, exactly now
        d2 = document(title='Aged 2')
        d2.render_expires = now
        d2.save()

        # Stale, a little while ago
        d3 = document(title='Aged 3')
        d3.render_expires = now - timedelta(seconds=100)
        d3.save()

        stale_docs = Document.objects.get_by_stale_rendering()
        eq_(sorted([d2.pk, d3.pk]),
            sorted([x.pk for x in stale_docs]))

    @override_config(KUMASCRIPT_TIMEOUT=1.0)
    @mock.patch('kuma.wiki.kumascript.get')
    def test_update_expires_with_max_age(self, mock_kumascript_get):
        mock_kumascript_get.return_value = ('MOCK CONTENT', None)

        max_age = 1000
        now = datetime.now()

        d1 = document(title='Aged 1')
        d1.render_max_age = max_age
        d1.save()
        d1.render()

        # HACK: Exact time comparisons suck, because execution time.
        later = now + timedelta(seconds=max_age)
        ok_(d1.render_expires > later - timedelta(seconds=1))
        ok_(d1.render_expires < later + timedelta(seconds=1))

    @override_config(KUMASCRIPT_TIMEOUT=1.0)
    @mock.patch('kuma.wiki.kumascript.get')
    def test_update_expires_without_max_age(self, mock_kumascript_get):
        mock_kumascript_get.return_value = ('MOCK CONTENT', None)

        now = datetime.now()
        d1 = document(title='Aged 1')
        d1.render_expires = now - timedelta(seconds=100)
        d1.save()
        d1.render()

        ok_(not d1.render_expires)

    @override_config(KUMASCRIPT_TIMEOUT=1.0)
    @mock.patch('kuma.wiki.kumascript.get')
    @mock.patch.object(tasks.render_document, 'delay')
    def test_render_stale(self, mock_render_document_delay,
                          mock_kumascript_get):
        mock_kumascript_get.return_value = ('MOCK CONTENT', None)

        now = datetime.now()
        earlier = now - timedelta(seconds=1000)

        d1 = document(title='Aged 3')
        d1.last_rendered_at = earlier
        d1.render_expires = now - timedelta(seconds=100)
        d1.save()

        tasks.render_stale_documents()

        d1_fresh = Document.objects.get(pk=d1.pk)
        ok_(not mock_render_document_delay.called)
        ok_(d1_fresh.last_rendered_at > earlier)


class PageMoveTests(UserTestCase):
    """Tests for page-moving and associated functionality."""

    @pytest.mark.move
    def test_children_simple(self):
        """A basic tree with two direct children and no sub-trees on
        either."""
        d1 = document(title='Parent', save=True)
        d2 = document(title='Child', save=True)
        d2.parent_topic = d1
        d2.save()
        d3 = document(title='Another child', save=True)
        d3.parent_topic = d1
        d3.save()

        eq_([d2, d3], d1.get_descendants())

    def test_get_descendants_limited(self):
        """Tests limiting of descendant levels"""
        def _make_doc(title, parent=None):
            doc = document(title=title, save=True)
            if parent:
                doc.parent_topic = parent
                doc.save()
            return doc

        parent = _make_doc('Parent')
        child1 = _make_doc('Child 1', parent)
        child2 = _make_doc('Child 2', parent)
        grandchild = _make_doc('GrandChild 1', child1)
        _make_doc('Great GrandChild 1', grandchild)

        # Test descendant counts
        eq_(len(parent.get_descendants()), 4)  # All
        eq_(len(parent.get_descendants(1)), 2)
        eq_(len(parent.get_descendants(2)), 3)
        eq_(len(parent.get_descendants(0)), 0)
        eq_(len(child2.get_descendants(10)), 0)
        eq_(len(grandchild.get_descendants(4)), 1)

    def test_children_complex(self):
        """A slightly more complex tree, with multiple children, some
        of which do/don't have their own children."""
        top = document(title='Parent', save=True)

        c1 = document(title='Child 1', save=True)
        c1.parent_topic = top
        c1.save()

        gc1 = document(title='Child of child 1', save=True)
        gc1.parent_topic = c1
        gc1.save()

        c2 = document(title='Child 2', save=True)
        c2.parent_topic = top
        c2.save()

        gc2 = document(title='Child of child 2', save=True)
        gc2.parent_topic = c2
        gc2.save()

        gc3 = document(title='Another child of child 2', save=True)
        gc3.parent_topic = c2
        gc3.save()

        ggc1 = document(title='Child of the second child of child 2',
                        save=True)

        ggc1.parent_topic = gc3
        ggc1.save()

        ok_([c1, gc1, c2, gc2, gc3, ggc1] == top.get_descendants())

    @pytest.mark.move
    def test_circular_dependency(self):
        """Make sure we can detect potential circular dependencies in
        parent/child relationships."""
        # Test detection at one level removed.
        parent = document(title='Parent of circular-dependency document',
                          save=True)
        child = document(title='Document with circular dependency')
        child.parent_topic = parent
        child.save()

        ok_(child.is_child_of(parent))

        # And at two levels removed.
        grandparent = document(title='Grandparent of '
                                     'circular-dependency document')
        parent.parent_topic = grandparent
        child.save()

        ok_(child.is_child_of(grandparent))

    @pytest.mark.move
    def test_move_tree(self):
        """Moving a tree of documents does the correct thing"""

        # Simple multi-level tree:
        #
        #  - top
        #    - child1
        #    - child2
        #      - grandchild
        top = revision(title='Top-level parent for tree moves',
                       slug='first-level/parent',
                       is_approved=True,
                       save=True)
        old_top_id = top.id
        top_doc = top.document

        child1 = revision(title='First child of tree-move parent',
                          slug='first-level/second-level/child1',
                          is_approved=True,
                          save=True)
        old_child1_id = child1.id
        child1_doc = child1.document
        child1_doc.parent_topic = top_doc
        child1_doc.save()

        child2 = revision(title='Second child of tree-move parent',
                          slug='first-level/second-level/child2',
                          is_approved=True,
                          save=True)
        old_child2_id = child2.id
        child2_doc = child2.document
        child2_doc.parent_topic = top_doc
        child2.save()

        grandchild = revision(title='Child of second child of tree-move parent',
                              slug='first-level/second-level/third-level/grandchild',
                              is_approved=True,
                              save=True)
        old_grandchild_id = grandchild.id
        grandchild_doc = grandchild.document
        grandchild_doc.parent_topic = child2_doc
        grandchild_doc.save()

        revision(title='New Top-level bucket for tree moves',
                 slug='new-prefix',
                 is_approved=True,
                 save=True)
        revision(title='New first-level parent for tree moves',
                 slug='new-prefix/first-level',
                 is_approved=True,
                 save=True)
        # Now we do a simple move: inserting a prefix that needs to be
        # inherited by the whole tree.
        top_doc._move_tree('new-prefix/first-level/parent')

        # And for each document verify three things:
        #
        # 1. The new slug is correct.
        # 2. A new revision was created when the page moved.
        # 3. A redirect was created.
        moved_top = Document.objects.get(pk=top_doc.id)
        eq_('new-prefix/first-level/parent',
            moved_top.current_revision.slug)
        ok_(old_top_id != moved_top.current_revision.id)
        ok_(moved_top.current_revision.slug in
            Document.objects.get(slug='first-level/parent').get_redirect_url())

        moved_child1 = Document.objects.get(pk=child1_doc.id)
        eq_('new-prefix/first-level/parent/child1',
            moved_child1.current_revision.slug)
        ok_(old_child1_id != moved_child1.current_revision.id)
        ok_(moved_child1.current_revision.slug in
            Document.objects.get(
                slug='first-level/second-level/child1'
            ).get_redirect_url())

        moved_child2 = Document.objects.get(pk=child2_doc.id)
        eq_('new-prefix/first-level/parent/child2',
            moved_child2.current_revision.slug)
        ok_(old_child2_id != moved_child2.current_revision.id)
        ok_(moved_child2.current_revision.slug in
            Document.objects.get(
                slug='first-level/second-level/child2'
            ).get_redirect_url())

        moved_grandchild = Document.objects.get(pk=grandchild_doc.id)
        eq_('new-prefix/first-level/parent/child2/grandchild',
            moved_grandchild.current_revision.slug)
        ok_(old_grandchild_id != moved_grandchild.current_revision.id)
        ok_(moved_grandchild.current_revision.slug in
            Document.objects.get(
                slug='first-level/second-level/third-level/grandchild'
            ).get_redirect_url())

    @pytest.mark.move
    def test_conflicts(self):
        top = revision(title='Test page-move conflict detection',
                       slug='test-move-conflict-detection',
                       is_approved=True,
                       save=True)
        top_doc = top.document
        child = revision(title='Child of conflict detection test',
                         slug='move-tests/conflict-child',
                         is_approved=True,
                         save=True)
        child_doc = child.document
        child_doc.parent_topic = top_doc
        child_doc.save()

        # We should find the conflict if it's at the slug the document
        # will move to.
        top_conflict = revision(title='Conflicting document for move conflict detection',
                                slug='moved/test-move-conflict-detection',
                                is_approved=True,
                                save=True)

        eq_([top_conflict.document],
            top_doc._tree_conflicts('moved/test-move-conflict-detection'))

        # Or if it will involve a child document.
        child_conflict = revision(title='Conflicting child for move conflict detection',
                                  slug='moved/test-move-conflict-detection/conflict-child',
                                  is_approved=True,
                                  save=True)

        eq_([top_conflict.document, child_conflict.document],
            top_doc._tree_conflicts('moved/test-move-conflict-detection'))

        # But a redirect should not trigger a conflict.
        revision(title='Conflicting document for move conflict detection',
                 slug='moved/test-move-conflict-detection',
                 content='REDIRECT <a class="redirect" href="/foo">Foo</a>',
                 document=top_conflict.document,
                 is_approved=True,
                 save=True)

        eq_([child_conflict.document],
            top_doc._tree_conflicts('moved/test-move-conflict-detection'))

    @pytest.mark.move
    def test_additional_conflicts(self):
        top = revision(title='WebRTC',
                       slug='WebRTC',
                       content='WebRTC',
                       is_approved=True,
                       save=True)
        top_doc = top.document
        child1 = revision(title='WebRTC Introduction',
                          slug='WebRTC/WebRTC_Introduction',
                          content='WebRTC Introduction',
                          is_approved=True,
                          save=True)
        child1_doc = child1.document
        child1_doc.parent_topic = top_doc
        child1_doc.save()
        child2 = revision(title='Taking webcam photos',
                          slug='WebRTC/Taking_webcam_photos',
                          is_approved=True,
                          save=True)
        child2_doc = child2.document
        child2_doc.parent_topic = top_doc
        child2_doc.save()
        eq_([],
            top_doc._tree_conflicts('NativeRTC'))

    @pytest.mark.move
    def test_preserve_tags(self):
            tags = "'moving', 'tests'"
            rev = revision(title='Test page-move tag preservation',
                           slug='page-move-tags',
                           tags=tags,
                           is_approved=True,
                           save=True)
            rev.review_tags.set('technical')
            rev = Revision.objects.get(pk=rev.id)

            revision(title='New Top-level parent for tree moves',
                     slug='new-top',
                     is_approved=True,
                     save=True)

            doc = rev.document
            doc._move_tree('new-top/page-move-tags')

            moved_doc = Document.objects.get(pk=doc.id)
            new_rev = moved_doc.current_revision
            eq_(tags, new_rev.tags)
            eq_(['technical'],
                [str(tag) for tag in new_rev.review_tags.all()])

    @pytest.mark.move
    def test_move_tree_breadcrumbs(self):
        """Moving a tree of documents under an existing doc updates breadcrumbs"""

        grandpa = revision(title='Top-level parent for breadcrumb move',
                           slug='grandpa', is_approved=True, save=True)
        grandpa_doc = grandpa.document

        dad = revision(title='Mid-level parent for breadcrumb move',
                       slug='grandpa/dad', is_approved=True, save=True)
        dad_doc = dad.document
        dad_doc.parent_topic = grandpa_doc
        dad_doc.save()

        son = revision(title='Bottom-level child for breadcrumb move',
                       slug='grandpa/dad/son', is_approved=True, save=True)
        son_doc = son.document
        son_doc.parent_topic = dad_doc
        son_doc.save()

        grandma = revision(title='Top-level parent for breadcrumb move',
                           slug='grandma', is_approved=True, save=True)
        grandma_doc = grandma.document

        mom = revision(title='Mid-level parent for breadcrumb move',
                       slug='grandma/mom', is_approved=True, save=True)
        mom_doc = mom.document
        mom_doc.parent_topic = grandma_doc
        mom_doc.save()

        daughter = revision(title='Bottom-level child for breadcrumb move',
                            slug='grandma/mom/daughter',
                            is_approved=True,
                            save=True)
        daughter_doc = daughter.document
        daughter_doc.parent_topic = mom_doc
        daughter_doc.save()

        # move grandma under grandpa
        grandma_doc._move_tree('grandpa/grandma')

        # assert the parent_topics are correctly rooted at grandpa
        # note we have to refetch these to see any DB changes.
        grandma_moved = Document.objects.get(locale=grandma_doc.locale,
                                             slug='grandpa/grandma')
        ok_(grandma_moved.parent_topic == grandpa_doc)
        mom_moved = Document.objects.get(locale=mom_doc.locale,
                                         slug='grandpa/grandma/mom')
        ok_(mom_moved.parent_topic == grandma_moved)

    @pytest.mark.move
    def test_move_tree_no_new_parent(self):
        """Moving a tree to a slug that doesn't exist throws error."""

        rev = revision(title='doc to move',
                       slug='doc1', is_approved=True, save=True)
        doc = rev.document

        try:
            doc._move_tree('slug-that-doesnt-exist/doc1')
            ok_(False, "Moving page under non-existing doc should error.")
        except Exception:
            pass

    @pytest.mark.move
    def test_move_top_level_docs(self):
        """Moving a top document to a new slug location"""
        page_to_move_title = 'Page Move Root'
        page_to_move_slug = 'Page_Move_Root'
        page_child_slug = 'Page_Move_Root/Page_Move_Child'
        page_moved_slug = 'Page_Move_Root_Moved'
        page_child_moved_slug = 'Page_Move_Root_Moved/Page_Move_Child'

        page_to_move_doc = document(title=page_to_move_title,
                                    slug=page_to_move_slug,
                                    save=True)
        rev = revision(document=page_to_move_doc,
                       title=page_to_move_title,
                       slug=page_to_move_slug,
                       save=True)
        page_to_move_doc.current_revision = rev
        page_to_move_doc.save()

        page_child = revision(title='child', slug=page_child_slug,
                              is_approved=True, save=True)
        page_child_doc = page_child.document
        page_child_doc.parent_topic = page_to_move_doc
        page_child_doc.save()

        # move page to new slug
        new_title = page_to_move_title + ' Moved'

        page_to_move_doc._move_tree(page_moved_slug, user=None,
                                    title=new_title)

        page_to_move_doc = Document.objects.get(slug=page_to_move_slug)
        page_moved_doc = Document.objects.get(slug=page_moved_slug)
        page_child_doc = Document.objects.get(slug=page_child_slug)
        page_child_moved_doc = Document.objects.get(slug=page_child_moved_slug)

        ok_('REDIRECT' in page_to_move_doc.html)
        ok_(page_moved_slug in page_to_move_doc.html)
        ok_(new_title in page_to_move_doc.html)
        ok_(page_moved_doc)
        ok_('REDIRECT' in page_child_doc.html)
        ok_(page_moved_slug in page_child_doc.html)
        ok_(page_child_moved_doc)
        # TODO: Fix this assertion?
        # eq_('admin', page_moved_doc.current_revision.creator.username)

    @pytest.mark.move
    def test_mid_move(self):
        root_title = 'Root'
        root_slug = 'Root'
        child_title = 'Child'
        child_slug = 'Root/Child'
        moved_child_slug = 'DiffChild'
        grandchild_title = 'Grandchild'
        grandchild_slug = 'Root/Child/Grandchild'
        moved_grandchild_slug = 'DiffChild/Grandchild'

        root_doc = document(title=root_title,
                            slug=root_slug,
                            save=True)
        rev = revision(document=root_doc,
                       title=root_title,
                       slug=root_slug,
                       save=True)
        root_doc.current_revision = rev
        root_doc.save()

        child = revision(title=child_title, slug=child_slug,
                         is_approved=True, save=True)
        child_doc = child.document
        child_doc.parent_topic = root_doc
        child_doc.save()

        grandchild = revision(title=grandchild_title,
                              slug=grandchild_slug,
                              is_approved=True, save=True)
        grandchild_doc = grandchild.document
        grandchild_doc.parent_topic = child_doc
        grandchild_doc.save()

        child_doc._move_tree(moved_child_slug)

        redirected_child = Document.objects.get(slug=child_slug)
        Document.objects.get(slug=moved_child_slug)
        ok_('REDIRECT' in redirected_child.html)
        ok_(moved_child_slug in redirected_child.html)

        redirected_grandchild = Document.objects.get(slug=grandchild_doc.slug)
        Document.objects.get(slug=moved_grandchild_slug)
        ok_('REDIRECT' in redirected_grandchild.html)
        ok_(moved_grandchild_slug in redirected_grandchild.html)

    @pytest.mark.move
    def test_move_special(self):
        root_slug = 'User:foo'
        child_slug = '%s/child' % root_slug

        new_root_slug = 'User:foobar'

        special_root = document(title='User:foo',
                                slug=root_slug,
                                save=True)
        revision(document=special_root,
                 title=special_root.title,
                 slug=root_slug,
                 save=True)

        special_child = document(title='User:foo child',
                                 slug=child_slug,
                                 save=True)
        revision(document=special_child,
                 title=special_child.title,
                 slug=child_slug,
                 save=True)

        special_child.parent_topic = special_root
        special_child.save()

        original_root_id = special_root.id
        original_child_id = special_child.id

        # First move, to new slug.
        special_root._move_tree(new_root_slug)

        # Appropriate redirects were left behind.
        root_redirect = Document.objects.get(locale=special_root.locale,
                                             slug=root_slug)
        ok_(root_redirect.is_redirect)
        root_redirect_id = root_redirect.id
        child_redirect = Document.objects.get(locale=special_child.locale,
                                              slug=child_slug)
        ok_(child_redirect.is_redirect)
        child_redirect_id = child_redirect.id

        # Moved documents still have the same IDs.
        moved_root = Document.objects.get(locale=special_root.locale,
                                          slug=new_root_slug)
        eq_(original_root_id, moved_root.id)
        moved_child = Document.objects.get(locale=special_child.locale,
                                           slug='%s/child' % new_root_slug)
        eq_(original_child_id, moved_child.id)

        # Second move, back to original slug.
        moved_root._move_tree(root_slug)

        # Once again we left redirects behind.
        root_second_redirect = Document.objects.get(locale=special_root.locale,
                                                    slug=new_root_slug)
        ok_(root_second_redirect.is_redirect)
        child_second_redirect = Document.objects.get(locale=special_child.locale,
                                                     slug='%s/child' % new_root_slug)
        ok_(child_second_redirect.is_redirect)

        # The documents at the original URLs aren't redirects anymore.
        rerooted_root = Document.objects.get(locale=special_root.locale,
                                             slug=root_slug)
        ok_(not rerooted_root.is_redirect)
        rerooted_child = Document.objects.get(locale=special_child.locale,
                                              slug=child_slug)
        ok_(not rerooted_child.is_redirect)

        # The redirects created in the first move no longer exist in the DB.
        self.assertRaises(Document.DoesNotExist,
                          Document.objects.get,
                          id=root_redirect_id)
        self.assertRaises(Document.DoesNotExist,
                          Document.objects.get,
                          id=child_redirect_id)

    def test_fail_message(self):
        """
        When page move fails in moving one of the children, it
        generates an informative exception message explaining which
        child document failed.

        """
        top = revision(title='Test page-move error messaging',
                       slug='test-move-error-messaging',
                       is_approved=True,
                       save=True)
        top_doc = top.document

        child = revision(title='Child to test page-move error messaging',
                         slug='test-move-error-messaging/child',
                         is_approved=True,
                         save=True)
        child_doc = child.document
        child_doc.parent_topic = top_doc
        child_doc.save()

        grandchild = revision(title='Grandchild to test page-move error handling',
                              slug='test-move-error-messaging/child/grandchild',
                              is_approved=True,
                              save=True)
        grandchild_doc = grandchild.document
        grandchild_doc.parent_topic = child_doc
        grandchild_doc.save()

        revision(title='Conflict page for page-move error handling',
                 slug='test-move-error-messaging/moved/grandchild',
                 is_approved=True,
                 save=True)
        # TODO: Someday when we're on Python 2.7, we can use
        # assertRaisesRegexp. Until then, we have to manually catch
        # and inspect the exception.
        try:
            child_doc._move_tree('test-move-error-messaging/moved')
        except PageMoveError as e:
            err_strings = [
                'with id %s' % grandchild_doc.id,
                'https://developer.mozilla.org/%s/docs/%s' % (grandchild_doc.locale,
                                                              grandchild_doc.slug),
                "Exception type: <type 'exceptions.Exception'>",
                'Exception message: Requested move would overwrite a non-redirect page.',
                'in _move_tree',
                'in _move_conflicts',
                'raise Exception("Requested move would overwrite a non-redirect page.")',
            ]
            for s in err_strings:
                ok_(s in e.args[0])


class DocumentParsingTests(UserTestCase):
    """Tests exercising content parsing methods"""

    def test_get_section_content(self):
        src = """
            <h2>Foo</h2>
            <p>Bar</p>
            <h3 id="Quick_Links">Quick Links</h3>
            <p>Foo, yay</p>
            <h2>Baz</h2>
            <p>Baz</p>
        """
        expected = """
            <p>Foo, yay</p>
        """

        r = revision(title='Document with sections',
                     slug='document-with-sections',
                     content=src,
                     is_approved=True, save=True)
        d = r.document

        result = d.get_section_content('Quick_Links')
        eq_(normalize_html(expected), normalize_html(result))

    def test_cached_content_fields(self):
        src = """
            <h2>First</h2>
            <p>This is a document</p>
            <h3 id="Quick_Links">Quick Links</h3>
            <p>Foo, yay</p>
            <h3 id="Subnav">Subnav</h3>
            <p>Bar, yay</p>
            <h2>Second</h2>
            <p>Another section</p>
            <a href="/en-US/docs/document-with-sections">Existing link</a>
            <a href="/en-US/docs/does-not-exist">New link</a>
        """
        body = """
            <h2 id="First">First</h2>
            <p>This is a document</p>
            <!-- -->
            <!-- -->
            <h2 id="Second">Second</h2>
            <p>Another section</p>
            <a href="/en-US/docs/document-with-sections">Existing link</a>
            <a class="new" href="/en-US/docs/does-not-exist">New link</a>
        """
        quick_links = """
            <p>Foo, yay</p>
        """
        subnav = """
            <p>Bar, yay</p>
        """

        r = revision(title='Document with sections',
                     slug='document-with-sections',
                     content=src,
                     is_approved=True, save=True)
        d = r.document

        eq_(normalize_html(body),
            normalize_html(d.get_body_html()))
        eq_(normalize_html(quick_links),
            normalize_html(d.get_quick_links_html()))
        eq_(normalize_html(subnav),
            normalize_html(d.get_zone_subnav_local_html()))

    def test_bug_982174(self):
        """Ensure build_json_data uses rendered HTML when available to extract
        sections generated by KumaScript (bug 982174)"""
        r = revision(title='Document with sections',
                     slug='document-with-sections',
                     is_approved=True, save=True)
        d = r.document

        # Save document with un-rendered content
        d.html = """
            <h2>Section 1</h2>
            <p>Foo</p>
            {{ h2_macro('Section 2') }}
            <p>Bar</p>
            <h2>Section 3</h2>
            <p>Foo</p>
        """
        d.save()
        json_data = d.build_json_data()
        expected_sections = [
            {'id': 'Section_1', 'title': 'Section 1'},
            {'id': 'Section_3', 'title': 'Section 3'}
        ]
        eq_(expected_sections, json_data['sections'])

        # Simulate kumascript rendering by updating rendered_html
        d.rendered_html = """
            <h2>Section 1</h2>
            <p>Foo</p>
            <h2>Section 2</h2>
            <p>Bar</p>
            <h2>Section 3</h2>
            <p>Foo</p>
        """
        d.save()
        json_data = d.build_json_data()
        expected_sections = [
            {'id': 'Section_1', 'title': 'Section 1'},
            {'id': 'Section_2', 'title': 'Section 2'},
            {'id': 'Section_3', 'title': 'Section 3'}
        ]
        eq_(expected_sections, json_data['sections'])


class RevisionIPTests(UserTestCase):
    def test_delete_older_than_default_30_days(self):
        old_date = date.today() - timedelta(days=31)
        r = revision(created=old_date, save=True)
        RevisionIP.objects.create(revision=r, ip='127.0.0.1').save()
        eq_(1, RevisionIP.objects.all().count())
        RevisionIP.objects.delete_old()
        eq_(0, RevisionIP.objects.all().count())

    def test_delete_older_than_days_argument(self):
        rev_date = date.today() - timedelta(days=5)
        r = revision(created=rev_date, save=True)
        RevisionIP.objects.create(revision=r, ip='127.0.0.1').save()
        eq_(1, RevisionIP.objects.all().count())
        RevisionIP.objects.delete_old(days=4)
        eq_(0, RevisionIP.objects.all().count())

    def test_delete_older_than_only_deletes_older_than(self):
        oldest_date = date.today() - timedelta(days=31)
        r1 = revision(created=oldest_date, save=True)
        RevisionIP.objects.create(revision=r1, ip='127.0.0.1').save()

        old_date = date.today() - timedelta(days=29)
        r1 = revision(created=old_date, save=True)
        RevisionIP.objects.create(revision=r1, ip='127.0.0.1').save()

        now_date = date.today()
        r2 = revision(created=now_date, save=True)
        RevisionIP.objects.create(revision=r2, ip='127.0.0.1').save()
        eq_(3, RevisionIP.objects.all().count())
        RevisionIP.objects.delete_old()
        eq_(2, RevisionIP.objects.all().count())


class AttachmentTests(UserTestCase):

    def new_attachment(self, mindtouch_attachment_id=666):
        attachment = Attachment(
            title='test attachment',
            mindtouch_attachment_id=mindtouch_attachment_id,
        )
        attachment.save()
        attachment_revision = AttachmentRevision(
            attachment=attachment,
            file='some/path.ext',
            mime_type='application/kuma',
            creator=get_user(username='admin'),
            title='test attachment',
        )
        attachment_revision.save()
        return attachment, attachment_revision

    def test_popuplate_deki_file_url(self):
        attachment, attachment_revision = self.new_attachment()
        html = ("""%s%s/@api/deki/files/%s/=""" %
                (settings.PROTOCOL, settings.ATTACHMENT_HOST,
                 attachment.mindtouch_attachment_id))
        doc = document(html=html, save=True)
        doc.populate_attachments()

        ok_(doc.attached_files.all().exists())
        eq_(doc.attached_files.all().count(), 1)
        eq_(doc.attached_files.first().file, attachment)

    def test_popuplate_kuma_file_url(self):
        attachment, attachment_revision = self.new_attachment()
        doc = document(html=attachment.get_file_url(), save=True)
        ok_(not doc.attached_files.all().exists())

        populated = doc.populate_attachments()
        eq_(len(populated), 1)
        ok_(doc.attached_files.all().exists())
        eq_(doc.attached_files.all().count(), 1)
        eq_(doc.attached_files.first().file, attachment)

    def test_popuplate_multiple_attachments(self):
        attachment, attachment_revision = self.new_attachment()
        attachment2, attachment_revision2 = self.new_attachment()
        html = ("%s %s" %
                (attachment.get_file_url(), attachment2.get_file_url()))
        doc = document(html=html, save=True)
        populated = doc.populate_attachments()
        attachments = doc.attached_files.all()
        eq_(len(populated), 2)
        ok_(attachments.exists())
        eq_(attachments.count(), 2)
        eq_(attachments[0].file, attachment)
        eq_(attachments[1].file, attachment2)
