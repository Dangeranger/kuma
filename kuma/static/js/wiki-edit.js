/*
 * wiki.js
 * Scripts for the wiki app.
 *
 */
(function ($, win, doc) {
    'use strict';

    /*
        Initialization of the CKEditor widget
    */
    (function() {
        var $textarea = $('#id_content');

        // CKEditor setup method
        var setup = function() {
          var $appBoxes = $('.approved .boxed');
          var $tools = $('div.cke_toolbox');
          var $container = $('.ckeditor-container');
          var $content = $('#cke_id_content');
          var contentTop = $container.offset().top;
          var fixed = false;

          // Switch header and toolbar styles on scroll to keep them on screen
          $(doc).on('scroll', function() {

            // If top of the window is betwen top of #content and bottom of content + 200, the header is fixed
            var scrollTop = $(this).scrollTop();
            if (scrollTop >= contentTop) {

              // Need to display or hide the toolbar depending on scroll position
               if(scrollTop > $container.height() + contentTop - 200 /* offset to ensure toolbar doesn't reach content bottom */) {
                $tools.css('display', 'none');
                return; // Cut off at some point
               }
               else {
                $tools.css('display', '');
               }

               // Fixed position toolbar if scrolled down to the editor
               // Wrapped in IF to cut down on processing
              if (!fixed) {
                fixed = true;
                $tools.css({
                  position: 'fixed',
                  top: 0,
                  width: $content.width() - 11
                });
              }

            } else { // If not, header is relative, put it back
              if (fixed) {
                fixed = false;
                $tools.css({
                  position: 'relative',
                  top: 'auto',
                  width: 'auto'
                });
              }
            }
          });

          $(win).resize(function() { // Recalculate box width on resize
            if (fixed) {
              $tools.css({
                width: $container.width() - 10
              }); // Readjust toolbox to fit
            }
          });
       };

      // Renders the WYSIWYG editor
      $textarea.each(function () {
        if (!$('body').is('.is-template')) {
          $(this).removeAttr('required').ckeditor(setup, {
            customConfig : '/en-US/docs/ckeditor_config.js'
          });
        }
      });
    })();

    /*
        Calculate rendering max age in seconds from days, minutes and seconds
    */
    (function() {
        var seconds = $('#id_render_max_age').val();
        var getValue = function(selector) {
             return parseInt($(selector).val()) || 0;
        };

        var setAge = function() {
            $('#id_render_max_age').val(
                (((getValue('.duration-container #days') * 24) +
                getValue('.duration-container #hours')) * 60 +
                getValue('.duration-container #minutes')) * 60
            );
        };

        $('.duration-container input').on('change', setAge);

        if(seconds !== ''){
            //convert seconds to days, hours, minutes
            var days = Math.floor(seconds / (60 * 60 * 24));
            seconds -= days * (60 * 60 * 24);
            var hours = Math.floor(seconds / (60 * 60));
            seconds -= hours * (60 * 60);
            var minutes = Math.floor(seconds / 60);

            $('.duration-container #days').val(days);
            $('.duration-container #hours').val(hours);
            $('.duration-container #minutes').val(minutes);
        }else{
            setAge();
        }
    })();

    /*
        Switch to source
    */
    (function(){
        $('.doc-mode-btn').toggleMessage().on('click', function(e){
            e.preventDefault();

            var $source = $('.translate-source textarea');
            $('.translate-rendered, .translate-source').toggleClass('hidden');

            // Get height of textarea content, first time doc source is viewed
            if(!$source.data('height')){
                 $source.height(function(){
                    return $(this).get(0).scrollHeight;
                 });
                 $source.data('height', true);
            }
        });

        $('.hide-original-btn').toggleMessage().on('click', function(e){
            e.preventDefault();

            $('#trans-content').toggleClass('translate-only');
        });
    })();

  /*
    Plugin for prepopulating the slug fields
  */
  $.fn.prepopulate = function(dependencies, maxLength) {
      var _changed = '_changed';

      return this.each(function() {
          var $field = $(this);

          $field.data(_changed, false);
          $field.on(_changed, function() {
              $field.data(_changed, true);
          });

          var populate = function () {
              // Bail if the fields value has changed
              if ($field.data(_changed) == true) return;

              var values = [], field_val, field_val_raw, split;
              dependencies.each(function() {
                  if ($(this).val().length > 0) {
                      values.push($(this).val());
                  }
              });

              var s = values.join(' ');
              s = $.slugifyString(s, false, true);

              // Trim to first num_chars chars
              s = s.substring(0, maxLength);

              // Only replace the last piece (don't replace slug heirarchy)
              split = $field.val().split('/');
              split[split.length - 1] = s;
              $field.val(split.join('/'));
          };

          dependencies.on('keyup change focus', populate);
      });
  };

  /*
    Functionality to set up the new, edit, and translate pages
  */
    var DRAFT_NAME;
    var DRAFT_TIMEOUT_ID;

    var supportsLocalStorage = win.mdn.features.localStorage;
    var $form = $('#wiki-page-edit');
    var isTranslation;
    var isTemplate;

    function init() {
        var $body = $('body');
        var HEADERS = [ 'HGROUP', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6' ];

        $('select.enable-if-js').removeAttr('disabled');

        // If the form is a translate form, update the $form object
        var $translateForm = $('#translate-document');
        if($translateForm.length) {
            $form = $translateForm;
            isTranslation = true;
        }

        if($body.hasClass('is-template')) {
            isTemplate = 1;
        }

        if ($body.is('.new')) {
            initPrepopulatedSlugs();
        }

        if ($body.is('.edit, .new, .translate')) {
            initMetadataEditButton();
            initSaveAndEditButtons();
            if(!$body.is('.is-template')) {
                initDirtinessTracking();
            }
            initArticlePreview();
            initAttachmentsActions();
            if(!isTemplate) {
                initDrafting();
            }
            initMetadataParentTranslation();
        }
        if ($body.is('.edit.is-template') || $body.is('.new.is-template')) {

            var textarea = $('textarea#id_content').hide();

            var editor = win.ace_editor = ace.edit('ace_content');
            editor.setTheme('ace/theme/dreamweaver');
            editor.setBehavioursEnabled(false);

            var JavaScriptMode = require('ace/mode/javascript').Mode;

            var session = editor.getSession();
            session.setMode(new JavaScriptMode());
            session.setValue(textarea.val());
            session.on('change', function(){
              textarea.val(editor.getSession().getValue());
            });
            $('.ace_text-input').focus();
            initDrafting();
        }
    }

    function initPrepopulatedSlugs() {
        var fields = {
            title: {
                id: '#id_slug',
                dependency_ids: ['#id_title'],
                dependency_list: ['#id_title'],
                maxLength: 50
            }
        };

        $.each(fields, function(i, field) {
            $(field.id).addClass('prepopulated_field');
            $(field.id).data('dependency_list', field.dependency_list)
                   .prepopulate($(field.dependency_ids.join(',')),
                                field.maxLength);
        });
    }

    /*
     * Initialize the article preview functionality.
     */
    function initArticlePreview() {
        $('.btn-preview').on('click', function(e) {
            e.preventDefault();

            // Ensure that content is available and exists
            var title = ' ';
            var $titleNode = $('#id_title');
            var data;

            if(CKEDITOR.instances['id_content']) {
                data = $.trim(CKEDITOR.instances['id_content'].getSnapshot());
            }
            else if(ace_editor && ace_editor) {
                data = $.trim(ace_editor.getSession().getValue());
            }
            else {
                return;
            }
            if($titleNode.length) {
                title = $titleNode.val();
            }

            // Since we have content, we can launch!
            if(data) {
                // Create and inject form for preview submission
                var $form = $("<form action='" + $(this).attr("data-preview-url") + "' target='previewWin' method='POST' />").appendTo(document.body);
                $("<input type='hidden' name='content' />").val(data).appendTo($form);
                $("<input type='hidden' name='title' />").val(title).appendTo($form);

                // Add the CSRF ?
                $('#wiki-page-edit, #translate-document').find('input[name=csrfmiddlewaretoken]').clone().appendTo($form);
                // Submit the form, and then get rid of it
                $form.get(0).submit();
                $form.remove();
            }

            return false;
        });
    }

    //
    // Initialize logic for metadata edit button.
    //
    function initMetadataEditButton () {

        if ($('#article-head .metadata').length) {
            var show_meta = function () {
                // Disable and hide the save-and-edit button when editing
                // metadata, since that can change the URL of the page and
                // tangle up where the iframe posts.
                $('#article-head .doc-title').hide();
                $('#article-head .metadata').show();
                $('#article-head .metadata #id_title').focus();
            }

            // Properties button reveals the metadata fields
            $('#btn-properties').on('click', function (ev) {
                ev.preventDefault();
                show_meta();
            });
            // Form errors reveal the metadata fields, since they're the most
            // likely culprits
            $('#edit-document .errorlist').each(show_meta);

        } else {
            $('#btn-properties').hide();
        }
    }

    //
    // Initialize logic for metadata parent translation
    //
    function initMetadataParentTranslation() {
        var $parentLis = $('.metadata-choose-parent');
        var $parentInput = $('#parent_id');

        $parentLis.each(function(index) {
            $(this).css('display', 'block');
            $('#parent_text').mozillaAutocomplete({
                minLength: 1,
                requireValidOption: true,
                autocompleteUrl: mdn.wiki.autosuggestTitleUrl,
                _renderItemAsLink: true,
                buildRequestData: function(req) {
                    req.locale = 'en-US';
                    return req;
                },
                onSelect: function(item, isSilent) {
                    $parentInput.val(item.id);
                },
                onDeselect: function(item) {
                    $parentInput.val('');
                }
            });
        });
    }

    //
    // Generates a storage key to be used by new, edit, translate, and translate-edit purposes
    // Ensures same key used by all functionalities in this file
    // Uses slashes as delimiters because they can't be used in slugs to edge name clashes based on
    // slug can be prevented
    //
    function getStorageKey() {
        var noEdit = location.pathname.replace('$edit', '');
        var finalKey;

        if(isTranslation) { // Translation interface
            finalKey = 'draft/translate' + noEdit + '/' + location.search.replace('?tolocale=', '');
            finalKey = finalKey.replace('$translate', '');
        }
        else if($('#id_current_rev').val()) { // Edit
            finalKey = 'draft/edit' + noEdit;
        }
        else { // New
            finalKey = 'draft/new';
        }

        // Add another identifier for templates
        if(isTemplate) {
            finalKey += '/template';
        }

        finalKey = $.trim(finalKey);
        return finalKey;
    }

    // Injects a DIV with language to the effect of "you had a previous draft, want to restore it?"
    // This takes the place of an ugly, ugly confirmation box :(
    var $draftDiv;
    function displayDraftBox(content, draft_time) {
        var draft_time_str = draft_time ? draft_time : gettext('an unknown date');
        var draft = gettext('You have a draft from:');
        var restore = gettext('Restore the draft content');
        var discard = gettext('Discard the draft');

        var text = draft + ' ' + draft_time_str + '. <a href="" class="restoreLink">' + restore + '</a>. <a href="" class="discardLink">'+ discard +'</a>.';
        var $contentNode = $('#id_content');
        var editor;

        // Plan the draft into the page
        $draftDiv = $('<div class="notice"><p>' + text + '</p></div>').insertBefore($contentNode);

        // Hook up the "restore" link
        $draftDiv.find('.restoreLink').on('click', function(e) {
            e.preventDefault();
            $contentNode.val(content);

            if(isTemplate) {
                editor = ace_editor;
                ace_editor.session.setValue(content);
            }
            else {
                editor = $contentNode.ckeditorGet();
                editor.setData(content);
            }
            editor.focus();

            updateDraftState('loaded');
            hideDraftBox();
        });

        // Hook up the "dispose" link
        $draftDiv.find('.discardLink').on('click', function(e) {
            e.preventDefault();
            hideDraftBox();
            clearDraft();
        });
    }
    function hideDraftBox() {
        $draftDiv && $draftDiv.css('display', 'none');
    }


    //
    // Initialize logic for save and save-and-edit buttons.
    //
    function initSaveAndEditButtons () {
        // Save button submits to top-level
        $('.btn-save').on('click', function () {
            if (supportsLocalStorage) {
                // Clear any preserved content.
                clearDraft();
            }
            clearTimeout(DRAFT_TIMEOUT_ID);
            $form.attr('action', '').removeAttr('target');
            return true;
        });

        // Save-and-edit submits to a hidden iframe, show loading message in notifier
        var notifications = [];
        $('.btn-save-and-edit').on('click', function () {

            notifications.push(mdn.Notifier.growl('Saving changes…', { duration: 0 }));

            mdn.analytics.trackEvent({
                category: 'Wiki',
                action: 'Button',
                label: 'Save and Keep Editing'
            });

            var savedTa = $form.find('textarea[name=content]').val();
            if (supportsLocalStorage) {
                // Preserve editor content, because saving to the iframe can
                // yield things like 403 / login-required errors that bust out
                // of the frame
                saveDraft(savedTa);
            }
            clearTimeout(DRAFT_TIMEOUT_ID);
            // Redirect the editor form to the iframe.
            $form.attr('action', '?iframe=1').attr('target', 'save-and-edit-target');
            return true;
        });
        $('.btn-save-and-edit').show();

        $('#save-and-edit-target').on('load', function () {
            if(notifications[0]) notifications[0].success(null, 2000);
            notifications.shift();

            if (supportsLocalStorage) {
                var if_doc = $(this).get(0).contentDocument;
                if (typeof(if_doc) != 'undefined') {

                    var ir = $('#iframe-response', if_doc);
                    if ('OK' == ir.attr('data-status')) {

                        // Dig into the iframe on load and look for "OK". If found,
                        // then it should be safe to throw away the preserved content.
                        localStorage.removeItem(DRAFT_NAME);

                        // We also need to update the form's current_rev to
                        // avoid triggering a conflict, since we just saved in
                        // the background.
                        $form.find('input[name=current_rev]').val(
                            ir.attr('data-current-revision'));

                    } else if ($form.add(if_doc).hasClass('conflict')) {
                        // HACK: If we detect a conflict in the iframe while
                        // doing save-and-edit, force a full-on save in order
                        // to surface the issue. There's no easy way to bust
                        // the iframe otherwise, since this was a POST.
                        $form.attr('action', '').attr('target', '');
                        $('.btn-save').click();

                    }

                    // Anything else that happens (eg. 403 errors) should have
                    // framebusting code to escape the hidden iframe.
                }
            }
            // Stop loading state on button
            $('.btn-save-and-edit').removeClass('loading');
            // Clear the review comment
            $('#id_comment').val('');
            // Re-enable the form; it gets disabled to prevent double-POSTs
            $form.data('disabled', false).removeClass('disabled');
            // Trigger a `mdn:save-success` event so dirtiness can be reset throughout the page
            $form.trigger('mdn:save-success');
            return true;
        });

        // Track submissions of the edit page form
        $form.on('submit', function() {
            mdn.optimizely.push(['trackEvent', 'editpage-submit']);
            mdn.analytics.trackEvent({
                category: 'Wiki',
                action: 'Form submission',
                label: 'Edit page'
            });
        });
    }

    function updateDraftState(action) {
        var now = new Date();
        var nowString = now.toLocaleDateString() + ' ' + now.toLocaleTimeString();

        $('#draft-action').text(action);
        $('#draft-time').attr('title', now.toISOString()).text(nowString);
    }

    function saveDraft(val) {
        if (supportsLocalStorage) {
            localStorage.setItem(DRAFT_NAME, val || $form.find('textarea[name=content]').val());
            var now = new Date();
            var nowString = now.toLocaleDateString() + ' ' + now.toLocaleTimeString();
            localStorage.setItem(DRAFT_NAME + '#save-time', nowString);
            updateDraftState(gettext('saved'));
        }
    }

    function clearDraft() {
        if (supportsLocalStorage) {
           localStorage.removeItem(DRAFT_NAME);
        }
    }

    function initDrafting() {
        var editor;
        DRAFT_NAME = getStorageKey();
        if (supportsLocalStorage) {
            var prev_draft = localStorage.getItem(DRAFT_NAME),
                treatDraft = function(content) {
                    return (content || '').replace(/ /g, '&nbsp;');
                },

                treatedDraft = $.trim(treatDraft(prev_draft)),
                treatedServer = treatDraft($form.find('textarea[name=content]').val().trim());
            if (prev_draft) {
                // draft matches server so discard draft
                if (treatedDraft == treatedServer) {
                    clearDraft();
                } else {
                    var draft_time = localStorage.getItem(DRAFT_NAME + '#save-time');
                    displayDraftBox(prev_draft, draft_time);
                }
            }
        }

        // Add key listener for CKEditor and drafting
        var callback = function() {
            clearTimeout(DRAFT_TIMEOUT_ID);
            DRAFT_TIMEOUT_ID = setTimeout(saveDraft, 3000);
        };
        if(isTemplate) {
            ace_editor.on && ace_editor.on('change', callback);
        }
        else {
            try {
                var $content = $('#id_content');
                $content.ckeditorGet && $content.ckeditorGet().on('key', callback);
            }
            catch(e) {
                console.log(e);
            }
        }

        // Clear draft upon discard
       $('.btn-discard').on('click', function() {
            clearTimeout(DRAFT_TIMEOUT_ID);
           clearDraft();
       });
    }

    function initAttachmentsActions() {
        var $attachmentsButton = $('#page-attachments-button');
        var $attachmentsNewTable = $('#page-attachments-new-table');
        var $attachmentsForm = $('#page-attachments-form');
        var $attachmentsFormCloneRow = $attachmentsNewTable.find('tbody tr').first();
        var $attachmentsNewTableActions = $attachmentsNewTable.find('tbody tr').last();
        var running = false;

        // If attachments are disabled, just hide the form
        if(!mdn.wiki.attachments_enabled) {
            $attachmentsButton.addClass('hidden');
            return;
        }

        // Upon click of the 'Attach Files' button, toggle display of upload table
        $attachmentsButton.on('click', function(e) {
            e.preventDefault();
            $attachmentsNewTable.toggleClass('hidden');
            if(!$attachmentsNewTable.hasClass('hidden')) {
                $attachmentsNewTable.find('input[type=text]').first()[0].focus();
            }
        });

        // Clicking the 'AMF' button adds more rows
        $('#page-attachments-more').on('click', function() {
            // Don't add boxes during submission
            if (running) {
              return;
            }
            function clone() {
                // Create and insert clone
                var $clone = $attachmentsFormCloneRow.clone();
                $clone.find('input, textarea').val('');
                $clone.find('.attachment-error').remove();
                $clone.insertBefore($attachmentsNewTableActions);
                $clone.addClass('dynamic-row');
                return $clone;
            }
            clone().find('input[type="text"]')[0].focus();
        });

        // Form submission, upload, and response handling
        $attachmentsForm.on('submit', function(e) {
            // Stop concurrent submissions
            if (running) {
              e.preventDefault();
              return;
            } else {
              running = true;
            }
        });
    }

    //
    // Initializes logic that keeps track of whether changes have been made to the article
    // So far three sections contribute to dirtiness: Metadata, editor content and tags
    //
    function initDirtinessTracking() {
      // These are all fields that count towards an edit, excluding the editor and tags
      var metaSelector = 'input:not([type="hidden"]), textarea, select';
      var $metaDataFields = $form.find(metaSelector);
      var editor = CKEDITOR.instances['id_content'];

      function setEditorButtonsEnabled(enabled) {
        var saveContinue = editor.getCommand('mdn-buttons-save');
        var saveEdit = editor.getCommand('mdn-buttons-save-exit');

        var state = CKEDITOR.TRISTATE_OFF;
        if (!enabled)
            state = CKEDITOR.TRISTATE_DISABLED;

        if (saveContinue)
            saveContinue.setState(state);
        if (saveEdit)
            saveEdit.setState(state);
      }

      function onDirty() {
        $('.btn-save-and-edit').attr('disabled', false);
        $('.btn-save').attr('disabled', false);
        setEditorButtonsEnabled(true);
      }
      // Called when everything is clean
      function onClean() {
        $('.btn-save-and-edit').attr('disabled', true);
        $('.btn-save').attr('disabled', true);
        setEditorButtonsEnabled(false);
      }

      function resetDirty() {
        editor.resetDirty();
        $metaDataFields.each(function() {
          var $this = $(this);
          var value = $this.val();

          if($this.attr('type') == 'checkbox') {
            value = this.checked;
          }

          $this.data('original', value);
        })
        $form.find('.dirty').removeClass('dirty');
        $form.trigger('mdn:clean');
      }

      // Three custom events are used to track changes throughout the page
      // Dirtiness is marked by the class `dirty`, cleanliness by `clean`
      $form.on('mdn:save-success', resetDirty)
      .on('mdn:dirty', onDirty)
      .on('mdn:clean', function() { // Gets triggered when a section is clean, others may still be dirty
        if (!$('.dirty').length)
          onClean();
      });

      // Keep track of editor dirtiness
      function checkEditorDirtiness() {
        var editorDirty = editor.checkDirty();

        if (editorDirty) {
          $form.find('.editor-container').addClass('dirty').trigger('mdn:dirty');
        } else {
          $form.find('.editor-container').removeClass('dirty').trigger('mdn:clean');
        }
      }

      var interval;
      editor.on('contentDom', function() {
        // Basic events we know trigger a change
        editor.document.on('keyup', checkEditorDirtiness);
        editor.on('paste setData', checkEditorDirtiness);

        // Since CKE doesn't provide us a change event yet, a constant check is still the best way to
        // determine if the editor has changed.
        if(interval) clearInterval(interval);
        interval = setInterval(checkEditorDirtiness, 1500); // 1 seconds is arbitrary, we can update as desired
      });
      editor.on('instanceReady', function(e) {
        if (e.editor == editor)
            setEditorButtonsEnabled(false);
      });

      $(win).on('beforeunload', function() {
        if(interval) clearInterval(interval);
      });


      // Keep track of metadata dirtiness
      $form.on('change input', metaSelector, function() {
        var $this = $(this);
        var value = $this.val();
        var typeAttr = $this.attr('type');

        if(typeAttr && typeAttr.toLowerCase() == 'checkbox') {
            value = this.checked;
        }

        if (value !== $this.data('original')) {
          if (!$this.hasClass('dirty')) {
            $this.addClass('dirty').trigger('mdn:dirty');
          }
        } else {
          $this.removeClass('dirty').trigger('mdn:clean');
        }
      });

      resetDirty();
    }

    $(doc).ready(init);

 }(jQuery, window, document));
