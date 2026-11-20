"""Starts a REAL LiveKit Agents job process the way AgentServer does on Linux (forkserver, same preload modules), from
the agent's own WorkerOptions: the entrypoint and prewarm are pickled into the child and prewarm runs there.
No LiveKit server is needed. Used by tests/test_voice_unit.py; usage: python spawn_job_process.py <dir with agent.py>"""
import asyncio, multiprocessing as mp, os, sys
sys.path.insert(0, sys.argv[1])
os.environ.setdefault("LIVEKIT_URL", "wss://example.livekit.cloud"); os.environ.setdefault("LIVEKIT_API_KEY", "k"); os.environ.setdefault("LIVEKIT_API_SECRET", "s")
import agent
from livekit.agents.ipc.proc_pool import ProcPool
from livekit.agents.job import JobExecutorType

async def main():
    opts = agent.build_worker()
    ctx = mp.get_context("forkserver")
    # exactly what AgentServer does on Linux (worker.py): preload the SDK warm-up modules, not __main__
    ctx.set_forkserver_preload(["livekit.plugins.silero", "livekit.agents.ipc._preload", "livekit.agents.ipc._preload_freeze"])
    pool = ProcPool(initialize_process_fnc=opts.prewarm_fnc, job_entrypoint_fnc=opts.entrypoint_fnc, session_end_fnc=None,
                    simulation_end_fnc=None, num_idle_processes=1, initialize_timeout=60, close_timeout=10, session_end_timeout=10,
                    inference_executor=None, job_executor_type=JobExecutorType.PROCESS, mp_ctx=ctx,
                    memory_warn_mb=0, memory_limit_mb=0, http_proxy=None, loop=asyncio.get_running_loop())
    ready = asyncio.Event()
    pool.on("process_ready", lambda proc: ready.set())
    await pool.start()
    try:
        await asyncio.wait_for(ready.wait(), 60)
        print("RESULT: job process started and prewarm completed", flush=True)
        await asyncio.sleep(3)
        print("MARK: idle, processes:", len(pool.processes), flush=True)
    except TimeoutError:
        print("RESULT: no job process became ready")
    finally:
        print("MARK: closing pool", flush=True)
        await pool.aclose()
        print("MARK: closed", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
    sys.stdout.flush(); os._exit(0)
