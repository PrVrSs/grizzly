# coding=utf-8
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
"""`ReduceManager` finds the smallest testcase(s) to reproduce an issue.
"""
from itertools import chain
from locale import LC_ALL, setlocale
from logging import getLogger
from math import ceil, log
from pathlib import Path
from shutil import rmtree
from tempfile import mkdtemp

from Collector.Collector import Collector
from FTB.Signatures.CrashInfo import CrashSignature
from sapphire import Sapphire

from ..common.reporter import FilesystemReporter, FuzzManagerReporter
from ..common.storage import TestCase, TestCaseLoadFailure, TestFile
from ..common.utils import grz_tmp
from ..main import configure_logging
from ..replay import ReplayManager, ReplayResult
from ..target import load as load_target, TargetLaunchError, TargetLaunchTimeout
from .exceptions import GrizzlyReduceBaseException, NotReproducible
from .stats import ReductionStats
from .strategies import STRATEGIES


__author__ = "Jesse Schwartzentruber"
__credits__ = ["Jesse Schwartzentruber", "Tyson Smith"]


LOG = getLogger(__name__)


def change_quality(crash_id, quality):
    """Update a FuzzManager crash entry quality (if the crash entry exists).

    Missing crash entries are ignored with a warning.

    Args:
        crash_id (int): Crash ID on FuzzManager server
        quality (int): Quality constant defined in FuzzManagerReporter.QUAL_*

    Raises:
        RuntimeError: Error communicating with FuzzManager server.

    Returns:
        None
    """
    LOG.info("Updating crash %d to quality %s", crash_id,
             FuzzManagerReporter.quality_name(quality))
    coll = Collector()

    url = "%s://%s:%d/crashmanager/rest/crashes/%d/" \
        % (coll.serverProtocol, coll.serverHost, coll.serverPort, crash_id)
    try:
        coll.patch(url, data={"testcase_quality": quality})
    except RuntimeError as exc:
        # let 404's go .. evidently the crash was deleted
        if str(exc) == "Unexpected HTTP response: 404":
            LOG.warning("Failed to update (404), does the crash still exist?")
        else:
            raise


class ReduceManager(object):
    """Manage reduction of one or more testcases to find the smallest testcase
    that reproduces a given issue.

    Attributes:
        ignore (list(str)): Classes of results to ignore (see `--ignore`).
        server (sapphire.Sapphire): Server instance to serve testcases.
        strategies (list(str)): List of strategies to use for reducing
                                testcases (in order).
        target (grizzly.target.Target): Target instance to run testcases.
        testcases (list(grizzly.common.storage.TestCase)): List of one or more Grizzly
                                                           testcases to reduce.
    """
    ANALYSIS_ITERATIONS = 11  # number of iterations to analyze
    ANALYSIS_MIN_CRASHES = 2  # --min-crashes value when analysis is used
    # probability that successful reduction will observe the crash
    ANALYSIS_TARGET_PROBABILITY = 0.95
    # to see the worst case, run the `repeat` calculation in run_reliability_analysis
    # using `crashes_percent = 1.0/ANALYSIS_ITERATIONS`

    IDLE_DELAY_MIN = 10
    IDLE_DELAY_DURATION_MULTIPLIER = 1.5
    ITER_TIMEOUT_MIN = 10
    ITER_TIMEOUT_DURATION_MULTIPLIER = 2

    def __init__(self, ignore, server, target, testcases, strategies, log_path,
                 tool=None, report_to_fuzzmanager=False, any_crash=False,
                 signature=None, use_harness=True, use_analysis=True,
                 static_timeout=False, idle_delay=0, idle_threshold=0):
        """Initialize reduction manager. Many arguments are common with `ReplayManager`.

        Args:
            ignore (list(str)): Value for `self.ignore` attribute.
            server (sapphire.Sapphire): Value for `self.server` attribute.
            target (grizzly.target.Target): Value for `self.target` attribute.
            testcases (list(grizzly.common.storage.TestCase)):
                Value for `self.testcases` attribute.
            strategies (list(str)): Value for `self.strategies` attribute.
            log_path (Path or str): Path to save results when reporting to filesystem.
            tool (str or None): Override tool when reporting to FuzzManager.
            report_to_fuzzmanager (bool): Report to FuzzManager rather than filesystem.
            any_crash (bool): Accept any crash when reducing, not just those matching
                              the specified or first observed signature.
            signature (FTB.Signatures.CrashInfo.CrashSignature or None):
                Signature for accepting crashes.
            use_harness (bool): Whether to allow use of harness when navigating
                                between testcases.
            use_analysis (bool): Analyse reliability of testcase before running each
                                 reduction strategy.
            static_timeout (bool): Use only specified timeouts (`--timeout` and
                                   `--idle-delay`), even if testcase appears to need
                                   less time.
            idle_delay (int): Number of seconds to wait before polling for idle.
            idle_threshold (int): CPU usage threshold to mark the process as idle.
        """
        self.ignore = ignore
        self.server = server
        self.strategies = strategies
        self.target = target
        self.testcases = testcases
        self._any_crash = any_crash
        # only coerce `log_path` to `Path` if it's a string
        # this caution is only necessary in python3.5 where pytest uses
        # pathlib2 rather than pathlib
        self._log_path = Path(log_path) if isinstance(log_path, str) else log_path
        # these parameters may be overwritten during analysis, so keep a copy of them
        self._original_relaunch = target.relaunch
        self._original_use_harness = use_harness
        self._report_to_fuzzmanager = report_to_fuzzmanager
        self._report_tool = tool
        self._signature = signature
        self._use_analysis = use_analysis
        self._use_harness = use_harness
        self._static_timeout = static_timeout
        self._idle_delay = idle_delay
        self._idle_threshold = idle_threshold
        self._stats = ReductionStats()

    def update_timeout(self, results):
        """Tune idle/server timeout values based on actual duration of expected results.

        Expected durations will be updated if the actual duration is much lower.

        Timeouts are not updated in three cases:

            - `static_timeout=True` is passed to constructor (`--static-timeout`),
            - `any_crash=True` is passed to constructor (`--any-crash`),
            - Target is running under valgrind (`--valgrind`).

        Arguments:
            results (grizzly.replay.ReplayResult):
                Observed results. Any given expected results may affect the idle delay
                and sapphire timeout.

        Returns:
            None
        """
        if (self._static_timeout or self._any_crash or
                getattr(self.target, "use_valgrind", False)):
            # the amount of time it can take to replay a test case can vary
            # when under Valgrind so do not update the timeout in that case

            # when any_crash is given, crashes may be completely unrelated (all are
            # expected), so lowering timeout or idle delay will only hide crashes
            return

        durations = list(chain.from_iterable(result.durations
                                             for result in results if result.expected))
        if not durations:
            # no expected results
            return
        run_time = max(durations)

        # If `run_time * multiplier` is less than idle poll delay, update it
        LOG.debug('Run time %r', run_time)
        new_idle_delay = max(self.IDLE_DELAY_MIN,
                             min(run_time * self.IDLE_DELAY_DURATION_MULTIPLIER,
                                 self._idle_delay))
        if new_idle_delay < self._idle_delay:
            LOG.info("Updating poll delay to: %r", new_idle_delay)
            self._idle_delay = new_idle_delay
        # If `run_time * multiplier` is less than iter_timeout, update it
        # in other words, decrease the timeout if this ran in less than half the timeout
        new_iter_timeout = max(self.ITER_TIMEOUT_MIN,
                               min(run_time * self.ITER_TIMEOUT_DURATION_MULTIPLIER,
                                   self.server.timeout))
        if new_iter_timeout < self.server.timeout:
            LOG.info("Updating max timeout to: %r", new_iter_timeout)
            self.server.timeout = new_iter_timeout

    def run_reliability_analysis(self, stats):
        """Run several analysis passes of the current testcase to find `run` parameters.

        The number of repetitions and minimum number of crashes are calculated to
        maximize the chances of observing the expected crash.

        Arguments:
            stats (list(object)): Opaque stats object. Increment the ".iters" attribute
                                  with the number of iterations performed.

        Returns:
            tuple(int, int): Values for `repeat` and `min_crashes` resulting from
                             analysis.
        """
        harness_crashes = 0
        non_harness_crashes = 0

        # Reset parameters.
        # Use repeat=1 & relaunch=ITERATIONS because this is closer to how we will run
        #   post-analysis.
        # We're only using repeat=1 instead of repeat=ITERATIONS so we can get feedback
        #   on every call to interesting.
        self.target.relaunch = self.ANALYSIS_ITERATIONS

        for use_harness in [True, False]:
            if use_harness and not self._original_use_harness:
                continue
            if not use_harness and harness_crashes == self.ANALYSIS_ITERATIONS:
                continue

            with ReplayManager(
                self.ignore, self.server, self.target, any_crash=self._any_crash,
                signature=self._signature, use_harness=use_harness,
            ) as replay:
                LOG.info("Running for %d iterations to assess reliability %s harness.",
                         self.ANALYSIS_ITERATIONS,
                         "using" if use_harness else "without")
                try:
                    results = replay.run(
                        self.testcases, repeat=self.ANALYSIS_ITERATIONS, min_results=1,
                        exit_early=False, idle_delay=self._idle_delay,
                        idle_threshold=self._idle_threshold,
                    )
                except (TargetLaunchError, TargetLaunchTimeout) as exc:
                    if isinstance(exc, TargetLaunchError) and exc.report:
                        self.report([ReplayResult(exc.report, None, [], False)],
                                    self.testcases, self._stats.copy(stats))
                        exc.report.cleanup()
                    raise
                try:
                    for stat in stats:
                        stat.iters += replay.status.iteration
                    self.update_timeout(results)
                    crashes = sum(x.count for x in results if x.expected)
                    self.report(
                        [result for result in results if not result.expected],
                        self.testcases, self._stats.copy(stats))
                    if crashes and use_harness:
                        harness_crashes = crashes
                    elif crashes:
                        non_harness_crashes = crashes
                finally:
                    for result in results:
                        result.report.cleanup()
                LOG.info("Testcase was interesting %0.1f%% of %d attempts %s harness.",
                         100.0 * crashes / self.ANALYSIS_ITERATIONS,
                         self.ANALYSIS_ITERATIONS,
                         "using" if use_harness else "without")
                # ensure same signature is always used
                self._signature = replay.signature

        if harness_crashes == 0 and non_harness_crashes == 0:
            raise NotReproducible("Did not reproduce during analysis")

        # should we use the harness? go with whichever crashed more
        self._use_harness = non_harness_crashes <= harness_crashes

        # this is max 99.9% to avoid domain errors in the calculation below
        crashes_percent = min(
            1.0 * max(non_harness_crashes, harness_crashes) / self.ANALYSIS_ITERATIONS,
            0.999)

        # adjust repeat/min-crashes depending on how reliable the testcase was
        min_crashes = self.ANALYSIS_MIN_CRASHES
        repeat = int(
            ceil(log(1 - self.ANALYSIS_TARGET_PROBABILITY, 1 - crashes_percent))
            * self.ANALYSIS_MIN_CRASHES)

        LOG.info("Analysis results:")
        if harness_crashes == self.ANALYSIS_ITERATIONS:
            LOG.info("* testcase was perfectly reliable with the harness (--no-harness "
                     "not assessed)")
        elif harness_crashes == non_harness_crashes:
            LOG.info("* testcase was equally reliable with/without the harness")
        elif not self._original_use_harness:
            LOG.info("* --no-harness was already set")
        else:
            LOG.info("* testcase was %s reliable with the harness",
                     "more" if harness_crashes > non_harness_crashes else "less")
        return (repeat, min_crashes)

    def testcase_size(self):
        """Calculate the current testcase size.

        Returns:
            int: Current size of the testcase(s).
        """
        return sum(tc.data_size for tc in self.testcases)

    def run(self, repeat=1, min_results=1):
        """Run testcase reduction.

        Args:
            repeat (int): Maximum number of times to run the TestCase.
            min_results (int): Minimum number of results needed before run can
                               be considered successful.

        Returns:
            int: One of the `FuzzManagerReporter.QUAL_*` constants.
        """
        any_success = False
        last_reports = None
        last_tried = None
        self._stats.add("init", self.testcase_size())
        # record total stats overall so that any time missed by individual milestones
        # will still be included in the total
        with self._stats.add_timed("final", self.testcase_size) as total_stats:
            if self._use_analysis:
                with self._stats.add_timed("analysis", self.testcase_size) as stats:
                    repeat, min_results = \
                        self.run_reliability_analysis([stats, total_stats])
                any_success = True  # analysis ran and didn't raise
            self.target.relaunch = min(self._original_relaunch, repeat)
            LOG.info("Repeat: %d, Minimum crashes: %d, Relaunch %d",
                     repeat, min_results, self.target.relaunch)
            for strategy_no, strategy in enumerate(self.strategies):
                LOG.info("")
                LOG.info("Using strategy %s (%d/%d)", strategy, strategy_no + 1,
                         len(self.strategies))
                replay = ReplayManager(self.ignore, self.server, self.target,
                                       any_crash=self._any_crash,
                                       signature=self._signature,
                                       use_harness=self._use_harness)
                strategy = STRATEGIES[strategy](self.testcases)
                if last_tried is not None:
                    strategy.update_tried(last_tried)
                    last_tried = None
                strategy_stats = self._stats.add_timed(strategy.name, self.testcase_size)
                best_results = []
                try:
                    with replay, strategy, strategy_stats:
                        for reduction in strategy:
                            strategy_stats.iters += replay.status.iteration
                            total_stats.iters += replay.status.iteration
                            keep_reduction = False
                            results = []
                            try:
                                # reduction is a new list of testcases to be
                                # replayed
                                results = replay.run(
                                    reduction,
                                    repeat=repeat,
                                    min_results=min_results,
                                    idle_delay=self._idle_delay,
                                    idle_threshold=self._idle_threshold)
                                self.update_timeout(results)
                                # get the first expected result (if any),
                                #   and update the strategy
                                first_expected = next((report for report in results
                                                       if report.expected), None)
                                success = first_expected is not None
                                served = None
                                if success and not self._any_crash:
                                    served = first_expected.served
                                strategy.update(success, served=served)
                                if strategy.name == "check" and not success:
                                    raise NotReproducible("Not reproducible at 'check'")
                                any_success = any_success or success
                                # if the reduction reproduced,
                                #   update self.testcases (new best)
                                if success:
                                    LOG.info("Reduction succeeded")
                                    for testcase in self.testcases:
                                        testcase.cleanup()
                                    self.testcases = reduction
                                    keep_reduction = True
                                    # cleanup old best results
                                    for result in best_results:
                                        result.report.cleanup()
                                    # filter expected results out into `best_results`
                                    best_results = [result for result in results
                                                    if result.expected]
                                    results = [result for result in results
                                               if not result.expected]
                                else:
                                    LOG.info("Attempt failed")
                                # if the reduction found other crashes,
                                #   report those immediately
                                self.report(
                                    results, reduction,
                                    self._stats.copy([strategy_stats, total_stats]))
                            except TargetLaunchError as exc:
                                if exc.report:
                                    self.report(
                                        [ReplayResult(exc.report, None, [], False)],
                                        reduction,
                                        self._stats.copy([strategy_stats, total_stats]))
                                    exc.report.cleanup()
                                raise
                            finally:
                                if not keep_reduction:
                                    for testcase in reduction:
                                        testcase.cleanup()
                                for result in results:
                                    result.report.cleanup()

                        # if self._signature was already set, this will do nothing
                        # otherwise, ensure the first found signature is used throughout
                        self._signature = replay.signature

                    last_reports = self.report(
                        best_results, self.testcases,
                        self._stats.copy([total_stats]))

                except KeyboardInterrupt:
                    if best_results:
                        last_reports = self.report(
                            best_results, self.testcases,
                            self._stats.copy([total_stats]))
                        LOG.warning("Ctrl+C detected, best reduction so far "
                                    "reported as %r", last_reports)
                    raise
                finally:
                    for result in best_results:
                        result.report.cleanup()

                # store "tried" cache to pass to next strategy
                last_tried = strategy.get_tried()

            # if we complete all strategies, mark the last reported crashes as reduced
            if self._report_to_fuzzmanager and last_reports:
                for crash_id in last_reports:
                    change_quality(crash_id, FuzzManagerReporter.QUAL_REDUCED_RESULT)

        # log a summary of what was done.
        LOG.info("Reduction summary:")
        for line in self._stats.format_lines():
            LOG.info(line)

        if any_success:
            return FuzzManagerReporter.QUAL_REDUCED_RESULT
        return FuzzManagerReporter.QUAL_NOT_REPRODUCIBLE

    def report(self, results, testcases, stats=None):
        """Report results, either to FuzzManager or to filesystem.

        Arguments:
            results (list(ReplayResult)): Results observed during reduction.
            testcases (list(TestCase)): Testcases used to trigger results.
            stats (ReductionStats): Statistics for reduction of these results.
                                    (may be different than `self._stats`)

        Returns:
            list(*): List of return values from `reporter.submit()`.
        """
        ret_values = []
        for result in results:
            if self._report_to_fuzzmanager:
                reporter = FuzzManagerReporter(self._report_tool)
                if result.expected:
                    reporter.force_report = True
            else:
                report_dir = "reports" if result.expected else "other_reports"
                reporter = FilesystemReporter(
                    report_path=str(self._log_path / report_dir),
                    major_bucket=False)
            # write reduction stats
            if stats is not None:
                report_path = Path(result.report.path)
                with (report_path / "reduce_stats.txt").open("w") as out:
                    for line in stats.format_lines():
                        print(line, file=out)
                (report_path / "reduce_stats.json").write_text(stats.json())
            # clone the tests so we can safely call purge_optional here for each report
            # (report.served may be different for non-expected or any-crash results)
            clones = [test.clone() for test in testcases]
            try:
                if result.served is not None:
                    for clone, served in zip(clones, result.served):
                        clone.purge_optional(served)
                ret_values.append(reporter.submit(clones, report=result.report))
            finally:
                for clone in clones:
                    clone.cleanup()
        return ret_values

    @classmethod
    def main(cls, args):
        """CLI for `grizzly.replay`.

        Arguments:
            args (argparse.Namespace): Result from `ReduceArgs.parse_args`.

        Returns:
            int: 0 for success. non-0 indicates a problem.
        """
        configure_logging(args.log_level)
        setlocale(LC_ALL, "")
        if args.fuzzmanager:
            FuzzManagerReporter.sanity_check(args.binary)

        LOG.info("Starting Grizzly Reduce")

        if args.ignore:
            LOG.info("Ignoring: %s", ", ".join(args.ignore))
        if args.xvfb:
            LOG.info("Running with Xvfb")
        if args.valgrind:
            LOG.info("Running with Valgrind. This will be SLOW!")
        if args.rr:
            LOG.info("Running with RR")

        target = None
        testcases = []
        tmp_prefs = None
        try:

            if args.sig:
                signature = CrashSignature.fromFile(args.sig)
            else:
                signature = None

            LOG.debug("loading the TestCases")
            try:
                testcases = TestCase.load(args.input, args.prefs is None)
                if not testcases:
                    raise TestCaseLoadFailure("Failed to load TestCases")
            except TestCaseLoadFailure as exc:
                LOG.error("Error: %s", str(exc))
                return FuzzManagerReporter.QUAL_NO_TESTCASE

            if args.no_harness:
                if len(testcases) > 1:
                    LOG.error("'--no-harness' cannot be used with multiple testcases")
                    return FuzzManagerReporter.QUAL_REDUCER_ERROR
                LOG.debug("--no-harness specified relaunch set to 1")
                args.relaunch = 1
            args.repeat = max(args.min_crashes, args.repeat)
            relaunch = min(args.relaunch, args.repeat)
            LOG.debug("initializing the Target")
            target = load_target(args.platform)(
                args.binary,
                args.extension,
                args.launch_timeout,
                args.log_limit,
                args.memory,
                relaunch,
                rr=args.rr,
                valgrind=args.valgrind,
                xvfb=args.xvfb)
            # prioritize specified prefs.js file over included file
            if args.prefs is not None:
                for testcase in testcases:
                    testcase.add_meta(TestFile.from_file(args.prefs, "prefs.js"))
                LOG.info("Using specified prefs.js")
                target.prefs = args.prefs
            else:
                for testcase in testcases:
                    prefs_tf = testcase.get_file("prefs.js")
                    if prefs_tf:
                        tmp_prefs = Path(mkdtemp(prefix="prefs_",
                                                 dir=grz_tmp("replay")))
                        prefs_tf.dump(str(tmp_prefs))
                        LOG.info("Using prefs.js from testcase")
                        target.prefs = str(tmp_prefs / "prefs.js")
                        break
            if testcases[0].env_vars.get("GRZ_FORCED_CLOSE") == "0":
                LOG.debug("setting target.forced_close=False")
                target.forced_close = False

            LOG.debug("starting sapphire server")
            # launch HTTP server used to serve test cases
            with Sapphire(auto_close=1, timeout=args.timeout) as server:
                target.reverse(server.port, server.port)
                mgr = ReduceManager(
                    args.ignore,
                    server,
                    target,
                    testcases,
                    args.strategies,
                    args.logs,
                    tool=args.tool,
                    report_to_fuzzmanager=args.fuzzmanager,
                    any_crash=args.any_crash,
                    signature=signature,
                    use_harness=not args.no_harness,
                    use_analysis=not args.no_analysis,
                    static_timeout=args.static_timeout,
                    idle_delay=args.idle_delay,
                    idle_threshold=args.idle_threshold)
                return_code = mgr.run(repeat=args.repeat, min_results=args.min_crashes)
            return return_code

        except (KeyboardInterrupt, TargetLaunchError, TargetLaunchTimeout):
            return FuzzManagerReporter.QUAL_REDUCER_ERROR

        except GrizzlyReduceBaseException as exc:
            LOG.error(exc.msg)
            return exc.code

        except Exception:  # noqa pylint: disable=broad-except
            LOG.exception("Exception during reduction!")
            return FuzzManagerReporter.QUAL_REDUCER_ERROR

        finally:
            LOG.warning("Shutting down...")
            if target is not None:
                target.cleanup()
            for testcase in testcases:
                testcase.cleanup()
            if tmp_prefs is not None:
                rmtree(str(tmp_prefs), ignore_errors=True)
            LOG.info("Done.")
