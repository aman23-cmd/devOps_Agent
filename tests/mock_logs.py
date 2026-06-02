"""
Mock GitHub Actions log generator for testing the Diagnosis Agent.
"""


def generate_mock_log(failure_category: str) -> str:
    """
    Returns realistic GitHub Actions log text for each failure category.
    Timestamps and step names simulate actual workflow output.
    """
    base_timestamp = "2026-05-25T10:15:"

    logs = {
        "flaky_test": f"""
{base_timestamp}01.123Z Run npm test
{base_timestamp}02.456Z > devops-agent@1.0.0 test
{base_timestamp}03.789Z > jest
{base_timestamp}05.012Z 
{base_timestamp}05.100Z FAIL tests/timing.test.js
{base_timestamp}05.150Z   ● API latency › should respond within 50ms
{base_timestamp}05.200Z 
{base_timestamp}05.250Z     AssertionError: expected latency to be < 50ms but got 120ms
{base_timestamp}05.300Z 
{base_timestamp}05.350Z       23 |     const start = Date.now();
{base_timestamp}05.400Z       24 |     await api.get('/ping');
{base_timestamp}05.450Z     > 25 |     expect(Date.now() - start).toBeLessThan(50);
{base_timestamp}05.500Z          |                                ^
{base_timestamp}05.550Z 
{base_timestamp}06.000Z Test Suites: 1 failed, 15 passed, 16 total
{base_timestamp}06.500Z Tests:       1 failed, 120 passed, 121 total
{base_timestamp}07.000Z Error: Process completed with exit code 1.
""",
        "dependency_issue": f"""
{base_timestamp}01.123Z Run python -m pytest
{base_timestamp}02.000Z ============================= test session starts ==============================
{base_timestamp}02.100Z platform linux -- Python 3.12.3, pytest-8.2.0, pluggy-1.5.0
{base_timestamp}02.200Z rootdir: /home/runner/work/repo/repo
{base_timestamp}02.300Z collected 0 items / 1 error
{base_timestamp}02.400Z 
{base_timestamp}02.500Z ==================================== ERRORS ====================================
{base_timestamp}02.600Z ________________________ ERROR collecting tests/test_main.py _________________________
{base_timestamp}02.700Z ImportError while importing test module '/home/runner/work/repo/repo/tests/test_main.py'.
{base_timestamp}02.800Z Hint: make sure your test modules/packages have valid Python names.
{base_timestamp}02.900Z Traceback:
{base_timestamp}03.000Z /opt/hostedtoolcache/Python/3.12.3/x64/lib/python3.12/importlib/__init__.py:90: in import_module
{base_timestamp}03.100Z     return _bootstrap._gcd_import(name[level:], package, level)
{base_timestamp}03.200Z tests/test_main.py:5: in <module>
{base_timestamp}03.300Z     import requests
{base_timestamp}03.400Z E   ModuleNotFoundError: No module named 'requests'
{base_timestamp}03.500Z =========================== short test summary info ============================
{base_timestamp}03.600Z ERROR tests/test_main.py
{base_timestamp}03.700Z !!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
{base_timestamp}03.800Z =============================== 1 error in 0.12s ===============================
{base_timestamp}04.000Z Error: Process completed with exit code 2.
""",
        "env_mismatch": f"""
{base_timestamp}01.123Z Run alembic upgrade head
{base_timestamp}02.456Z INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.
{base_timestamp}02.500Z INFO  [alembic.runtime.migration] Will assume transactional DDL.
{base_timestamp}02.600Z Traceback (most recent call last):
{base_timestamp}02.700Z   File "/opt/venv/bin/alembic", line 8, in <module>
{base_timestamp}02.800Z     sys.exit(main())
{base_timestamp}02.900Z   File "/opt/venv/lib/python3.12/site-packages/alembic/config.py", line 640, in main
{base_timestamp}03.000Z     CommandLine(prog=prog).main(argv=argv)
{base_timestamp}03.100Z   File "/opt/venv/lib/python3.12/site-packages/alembic/config.py", line 634, in main
{base_timestamp}03.200Z     self.run_cmd(cfg, options)
{base_timestamp}03.300Z   File "/opt/venv/lib/python3.12/site-packages/alembic/config.py", line 611, in run_cmd
{base_timestamp}03.400Z     fn(
{base_timestamp}03.500Z   File "/opt/venv/lib/python3.12/site-packages/alembic/command.py", line 403, in upgrade
{base_timestamp}03.600Z     script.run_env()
{base_timestamp}03.700Z   File "/opt/venv/lib/python3.12/site-packages/alembic/script/base.py", line 583, in run_env
{base_timestamp}03.800Z     util.load_python_file(self.dir, "env.py")
{base_timestamp}03.900Z   File "/opt/venv/lib/python3.12/site-packages/alembic/util/pyfiles.py", line 95, in load_python_file
{base_timestamp}04.000Z     module = load_module_py(module_id, path)
{base_timestamp}04.100Z   File "/opt/venv/lib/python3.12/site-packages/alembic/util/pyfiles.py", line 113, in load_module_py
{base_timestamp}04.200Z     spec.loader.exec_module(module)  # type: ignore
{base_timestamp}04.300Z   File "<frozen importlib._bootstrap_external>", line 995, in exec_module
{base_timestamp}04.400Z   File "<frozen importlib._bootstrap>", line 488, in _call_with_frames_removed
{base_timestamp}04.500Z   File "alembic/env.py", line 21, in <module>
{base_timestamp}04.600Z     db_url = os.environ['DATABASE_URL']
{base_timestamp}04.700Z   File "/opt/hostedtoolcache/Python/3.12.3/x64/lib/python3.12/os.py", line 685, in __getitem__
{base_timestamp}04.800Z     raise KeyError(key) from None
{base_timestamp}04.900Z KeyError: 'DATABASE_URL'
{base_timestamp}05.000Z Error: Process completed with exit code 1.
""",
        "resource_exhaustion": f"""
{base_timestamp}01.123Z Run npm run build
{base_timestamp}02.000Z > next build
{base_timestamp}02.500Z info  - Loaded env from /home/runner/work/repo/repo/.env.production
{base_timestamp}03.000Z info  - Checking validity of types...
{base_timestamp}15.000Z info  - Creating an optimized production build...
{base_timestamp}45.000Z info  - Compiled successfully
{base_timestamp}46.000Z info  - Collecting page data...
{base_timestamp}55.000Z 
{base_timestamp}56.000Z <--- Last few GCs --->
{base_timestamp}57.000Z [1234:0x555555555555]    45000 ms: Mark-sweep 2048.0 (2050.5) -> 2047.5 (2050.5) MB, 150.0 / 0.0 ms  (average mu = 0.123, current mu = 0.005) allocation failure scavenge might not succeed
{base_timestamp}58.000Z [1234:0x555555555555]    45150 ms: Mark-sweep 2048.5 (2050.5) -> 2048.0 (2050.5) MB, 145.0 / 0.0 ms  (average mu = 0.050, current mu = 0.005) allocation failure scavenge might not succeed
{base_timestamp}59.000Z 
{base_timestamp}60.000Z <--- JS stacktrace --->
{base_timestamp}61.000Z FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory
{base_timestamp}62.000Z  1: 0x555555555555 node::Abort() [node]
{base_timestamp}63.000Z  2: 0x555555555556 node::FatalError(char const*, char const*) [node]
{base_timestamp}64.000Z  3: 0x555555555557 v8::Utils::ReportOOMFailure(v8::internal::Isolate*, char const*, bool) [node]
{base_timestamp}65.000Z  4: 0x555555555558 v8::internal::V8::FatalProcessOutOfMemory(v8::internal::Isolate*, char const*, bool) [node]
{base_timestamp}66.000Z Aborted (core dumped)
{base_timestamp}67.000Z Error: Process completed with exit code 134.
""",
        "network_timeout": f"""
{base_timestamp}01.123Z Run docker pull myregistry.com/myimage:latest
{base_timestamp}02.000Z latest: Pulling from myimage
{base_timestamp}17.000Z error pulling image configuration: download failed after attempts=6: dial tcp 192.168.1.100:443: connect: connection timed out
{base_timestamp}18.000Z Error: Process completed with exit code 1.
""",
        "code_regression": f"""
{base_timestamp}01.123Z Run go test ./...
{base_timestamp}02.000Z ?       github.com/org/repo/cmd    [no test files]
{base_timestamp}03.000Z === RUN   TestCalculateTotal
{base_timestamp}03.100Z     calculator_test.go:45: 
{base_timestamp}03.200Z         	Error Trace:	/home/runner/work/repo/repo/pkg/calc/calculator_test.go:45
{base_timestamp}03.300Z         	Error:      	Not equal: 
{base_timestamp}03.400Z         	            	expected: 100
{base_timestamp}03.500Z         	            	actual  : 0
{base_timestamp}03.600Z         	Test:       	TestCalculateTotal
{base_timestamp}03.700Z --- FAIL: TestCalculateTotal (0.00s)
{base_timestamp}04.000Z FAIL
{base_timestamp}04.100Z FAIL	github.com/org/repo/pkg/calc	0.123s
{base_timestamp}05.000Z Error: Process completed with exit code 1.
""",
        "config_error": f"""
{base_timestamp}00.100Z Setting up job
{base_timestamp}00.200Z Error: .github/workflows/ci.yml: (Line: 45, Col: 14):
{base_timestamp}00.300Z Error: A sequence was not expected
{base_timestamp}00.400Z Error: .github/workflows/ci.yml: (Line: 46, Col: 14):
{base_timestamp}00.500Z Error: Unexpected value 'run'
{base_timestamp}00.600Z Error: GitHub Actions encountered a YAML syntax error in the workflow file.
""",
        "infrastructure_failure": f"""
{base_timestamp}01.123Z Run setup-node
{base_timestamp}02.000Z Current runner version: '2.311.0'
{base_timestamp}03.000Z Runner lost communication with the server.
{base_timestamp}04.000Z Error: The operation was canceled.
""",
        "unknown": f"""
{base_timestamp}01.123Z Run custom script
{base_timestamp}02.000Z Starting deployment...
{base_timestamp}03.000Z Failed.
{base_timestamp}04.000Z Error: Process completed with exit code 1.
""",
    }

    return logs.get(failure_category.lower(), logs["unknown"])
