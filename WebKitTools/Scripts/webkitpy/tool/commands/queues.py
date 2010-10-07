# Copyright (c) 2009 Google Inc. All rights reserved.
# Copyright (c) 2009 Apple Inc. All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
# 
#     * Redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above
# copyright notice, this list of conditions and the following disclaimer
# in the documentation and/or other materials provided with the
# distribution.
#     * Neither the name of Google Inc. nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import time
import traceback
import os

from datetime import datetime
from optparse import make_option
from StringIO import StringIO

from webkitpy.common.net.bugzilla import CommitterValidator
from webkitpy.common.net.statusserver import StatusServer
from webkitpy.common.system.executive import ScriptError
from webkitpy.common.system.deprecated_logging import error, log
from webkitpy.tool.commands.stepsequence import StepSequenceErrorHandler
from webkitpy.tool.bot.commitqueuetask import CommitQueueTask
from webkitpy.tool.bot.feeders import CommitQueueFeeder
from webkitpy.tool.bot.patchcollection import PersistentPatchCollection, PersistentPatchCollectionDelegate
from webkitpy.tool.bot.queueengine import QueueEngine, QueueEngineDelegate
from webkitpy.tool.grammar import pluralize
from webkitpy.tool.multicommandtool import Command, TryAgain

class AbstractQueue(Command, QueueEngineDelegate):
    watchers = [
    ]

    _pass_status = "Pass"
    _fail_status = "Fail"
    _retry_status = "Retry"
    _error_status = "Error"

    def __init__(self, options=None): # Default values should never be collections (like []) as default values are shared between invocations
        options_list = (options or []) + [
            make_option("--no-confirm", action="store_false", dest="confirm", default=True, help="Do not ask the user for confirmation before running the queue.  Dangerous!"),
            make_option("--exit-after-iteration", action="store", type="int", dest="iterations", default=None, help="Stop running the queue after iterating this number of times."),
        ]
        Command.__init__(self, "Run the %s" % self.name, options=options_list)
        self._iteration_count = 0

    def _cc_watchers(self, bug_id):
        try:
            self._tool.bugs.add_cc_to_bug(bug_id, self.watchers)
        except Exception, e:
            traceback.print_exc()
            log("Failed to CC watchers.")

    def run_webkit_patch(self, args):
        webkit_patch_args = [self._tool.path()]
        # FIXME: This is a hack, we should have a more general way to pass global options.
        # FIXME: We must always pass global options and their value in one argument
        # because our global option code looks for the first argument which does
        # not begin with "-" and assumes that is the command name.
        webkit_patch_args += ["--status-host=%s" % self._tool.status_server.host]
        webkit_patch_args.extend(args)
        return self._tool.executive.run_and_throw_if_fail(webkit_patch_args)

    def _log_directory(self):
        return "%s-logs" % self.name

    # QueueEngineDelegate methods

    def queue_log_path(self):
        return os.path.join(self._log_directory(), "%s.log" % self.name)

    def work_item_log_path(self, work_item):
        raise NotImplementedError, "subclasses must implement"

    def begin_work_queue(self):
        log("CAUTION: %s will discard all local changes in \"%s\"" % (self.name, self._tool.scm().checkout_root))
        if self.options.confirm:
            response = self._tool.user.prompt("Are you sure?  Type \"yes\" to continue: ")
            if (response != "yes"):
                error("User declined.")
        log("Running WebKit %s." % self.name)
        self._tool.status_server.update_status(self.name, "Starting Queue")

    def stop_work_queue(self, reason):
        self._tool.status_server.update_status(self.name, "Stopping Queue, reason: %s" % reason)

    def should_continue_work_queue(self):
        self._iteration_count += 1
        return not self.options.iterations or self._iteration_count <= self.options.iterations

    def next_work_item(self):
        raise NotImplementedError, "subclasses must implement"

    def should_proceed_with_work_item(self, work_item):
        raise NotImplementedError, "subclasses must implement"

    def process_work_item(self, work_item):
        raise NotImplementedError, "subclasses must implement"

    def handle_unexpected_error(self, work_item, message):
        raise NotImplementedError, "subclasses must implement"

    # Command methods

    def execute(self, options, args, tool, engine=QueueEngine):
        self.options = options  # FIXME: This code is wrong.  Command.options is a list, this assumes an Options element!
        self._tool = tool  # FIXME: This code is wrong too!  Command.bind_to_tool handles this!
        return engine(self.name, self, self._tool.wakeup_event).run()

    @classmethod
    def _log_from_script_error_for_upload(cls, script_error, output_limit=None):
        # We have seen request timeouts with app engine due to large
        # log uploads.  Trying only the last 512k.
        if not output_limit:
            output_limit = 512 * 1024  # 512k
        output = script_error.message_with_output(output_limit=output_limit)
        # We pre-encode the string to a byte array before passing it
        # to status_server, because ClientForm (part of mechanize)
        # wants a file-like object with pre-encoded data.
        return StringIO(output.encode("utf-8"))

    @classmethod
    def _update_status_for_script_error(cls, tool, state, script_error, is_error=False):
        message = str(script_error)
        if is_error:
            message = "Error: %s" % message
        failure_log = cls._log_from_script_error_for_upload(script_error)
        return tool.status_server.update_status(cls.name, message, state["patch"], failure_log)


class FeederQueue(AbstractQueue):
    name = "feeder-queue"

    _sleep_duration = 30  # seconds

    # AbstractPatchQueue methods

    def begin_work_queue(self):
        AbstractQueue.begin_work_queue(self)
        self.feeders = [
            CommitQueueFeeder(self._tool),
        ]

    def next_work_item(self):
        # This really show inherit from some more basic class that doesn't
        # understand work items, but the base class in the heirarchy currently
        # understands work items.
        return "synthetic-work-item"

    def should_proceed_with_work_item(self, work_item):
        return True

    def process_work_item(self, work_item):
        for feeder in self.feeders:
            feeder.feed()
        time.sleep(self._sleep_duration)
        return True

    def work_item_log_path(self, work_item):
        return None

    def handle_unexpected_error(self, work_item, message):
        log(message)


class AbstractPatchQueue(AbstractQueue):
    def _update_status(self, message, patch=None, results_file=None):
        return self._tool.status_server.update_status(self.name, message, patch, results_file)

    def _fetch_next_work_item(self):
        return self._tool.status_server.next_work_item(self.name)

    def _did_pass(self, patch):
        self._update_status(self._pass_status, patch)

    def _did_fail(self, patch):
        self._update_status(self._fail_status, patch)

    def _did_retry(self, patch):
        self._update_status(self._retry_status, patch)

    def _did_error(self, patch, reason):
        message = "%s: %s" % (self._error_status, reason)
        self._update_status(message, patch)

    def work_item_log_path(self, patch):
        return os.path.join(self._log_directory(), "%s.log" % patch.bug_id())


class CommitQueue(AbstractPatchQueue, StepSequenceErrorHandler):
    name = "commit-queue"

    # AbstractPatchQueue methods

    def begin_work_queue(self):
        AbstractPatchQueue.begin_work_queue(self)
        self.committer_validator = CommitterValidator(self._tool.bugs)

    def next_work_item(self):
        patch_id = self._fetch_next_work_item()
        if not patch_id:
            return None
        return self._tool.bugs.fetch_attachment(patch_id)

    def should_proceed_with_work_item(self, patch):
        patch_text = "rollout patch" if patch.is_rollout() else "patch"
        self._update_status("Processing %s" % patch_text, patch)
        return True

    def process_work_item(self, patch):
        self._cc_watchers(patch.bug_id())
        task = CommitQueueTask(self._tool, self, patch)
        try:
            if task.run():
                self._did_pass(patch)
                return True
            self._did_retry(patch)
        except ScriptError, e:
            validator = CommitterValidator(self._tool.bugs)
            validator.reject_patch_from_commit_queue(patch.id(), self._error_message_for_bug(task.failure_status_id, e))
            self._did_fail(patch)

    def handle_unexpected_error(self, patch, message):
        self.committer_validator.reject_patch_from_commit_queue(patch.id(), message)

    def command_passed(self, message, patch):
        self._update_status(message, patch=patch)

    def command_failed(self, message, script_error, patch):
        failure_log = self._log_from_script_error_for_upload(script_error)
        return self._update_status(message, patch=patch, results_file=failure_log)

    def _error_message_for_bug(self, status_id, script_error):
        if not script_error.output:
            return script_error.message_with_output()
        results_link = self._tool.status_server.results_url_for_status(status_id)
        return "%s\nFull output: %s" % (script_error.message_with_output(), results_link)

    # StepSequenceErrorHandler methods

    def handle_script_error(cls, tool, state, script_error):
        # Hitting this error handler should be pretty rare.  It does occur,
        # however, when a patch no longer applies to top-of-tree in the final
        # land step.
        log(script_error.message_with_output())

    @classmethod
    def handle_checkout_needs_update(cls, tool, state, options, error):
        message = "Tests passed, but commit failed (checkout out of date).  Updating, then landing without building or re-running tests."
        tool.status_server.update_status(cls.name, message, state["patch"])
        # The only time when we find out that out checkout needs update is
        # when we were ready to actually pull the trigger and land the patch.
        # Rather than spinning in the master process, we retry without
        # building or testing, which is much faster.
        options.build = False
        options.test = False
        options.update = True
        raise TryAgain()


class RietveldUploadQueue(AbstractPatchQueue, StepSequenceErrorHandler):
    name = "rietveld-upload-queue"

    def __init__(self):
        AbstractPatchQueue.__init__(self)

    # AbstractPatchQueue methods

    def next_work_item(self):
        patch_id = self._tool.bugs.queries.fetch_first_patch_from_rietveld_queue()
        if patch_id:
            return patch_id
        self._update_status("Empty queue")

    def should_proceed_with_work_item(self, patch):
        self._update_status("Uploading patch", patch)
        return True

    def process_work_item(self, patch):
        try:
            self.run_webkit_patch(["post-attachment-to-rietveld", "--force-clean", "--non-interactive", "--parent-command=rietveld-upload-queue", patch.id()])
            self._did_pass(patch)
            return True
        except ScriptError, e:
            if e.exit_code != QueueEngine.handled_error_code:
                self._did_fail(patch)
            raise e

    @classmethod
    def _reject_patch(cls, tool, patch_id):
        tool.bugs.set_flag_on_attachment(patch_id, "in-rietveld", "-")

    def handle_unexpected_error(self, patch, message):
        log(message)
        self._reject_patch(self._tool, patch.id())

    # StepSequenceErrorHandler methods

    @classmethod
    def handle_script_error(cls, tool, state, script_error):
        log(script_error.message_with_output())
        cls._update_status_for_script_error(tool, state, script_error)
        cls._reject_patch(tool, state["patch"].id())


class AbstractReviewQueue(AbstractPatchQueue, PersistentPatchCollectionDelegate, StepSequenceErrorHandler):
    def __init__(self, options=None):
        AbstractPatchQueue.__init__(self, options)

    def review_patch(self, patch):
        raise NotImplementedError, "subclasses must implement"

    # PersistentPatchCollectionDelegate methods

    def collection_name(self):
        return self.name

    def fetch_potential_patch_ids(self):
        return self._tool.bugs.queries.fetch_attachment_ids_from_review_queue()

    def status_server(self):
        return self._tool.status_server

    def is_terminal_status(self, status):
        return status == "Pass" or status == "Fail" or status.startswith("Error:")

    # AbstractPatchQueue methods

    def begin_work_queue(self):
        AbstractPatchQueue.begin_work_queue(self)
        self._patches = PersistentPatchCollection(self)

    def next_work_item(self):
        patch_id = self._patches.next()
        if patch_id:
            return self._tool.bugs.fetch_attachment(patch_id)

    def should_proceed_with_work_item(self, patch):
        raise NotImplementedError, "subclasses must implement"

    def process_work_item(self, patch):
        try:
            if not self.review_patch(patch):
                return False
            self._did_pass(patch)
            return True
        except ScriptError, e:
            if e.exit_code != QueueEngine.handled_error_code:
                self._did_fail(patch)
            raise e

    def handle_unexpected_error(self, patch, message):
        log(message)

    # StepSequenceErrorHandler methods

    @classmethod
    def handle_script_error(cls, tool, state, script_error):
        log(script_error.message_with_output())


class StyleQueue(AbstractReviewQueue):
    name = "style-queue"
    def __init__(self):
        AbstractReviewQueue.__init__(self)

    def should_proceed_with_work_item(self, patch):
        self._update_status("Checking style", patch)
        return True

    def review_patch(self, patch):
        self.run_webkit_patch(["check-style", "--force-clean", "--non-interactive", "--parent-command=style-queue", patch.id()])
        return True

    @classmethod
    def handle_script_error(cls, tool, state, script_error):
        is_svn_apply = script_error.command_name() == "svn-apply"
        status_id = cls._update_status_for_script_error(tool, state, script_error, is_error=is_svn_apply)
        if is_svn_apply:
            QueueEngine.exit_after_handled_error(script_error)
        message = "Attachment %s did not pass %s:\n\n%s\n\nIf any of these errors are false positives, please file a bug against check-webkit-style." % (state["patch"].id(), cls.name, script_error.message_with_output(output_limit=3*1024))
        tool.bugs.post_comment_to_bug(state["patch"].bug_id(), message, cc=cls.watchers)
        exit(1)
