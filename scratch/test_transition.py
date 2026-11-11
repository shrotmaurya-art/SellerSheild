import asyncio, sys
sys.path.insert(0, 'agent/src')
from sellershield_agent.config import get_settings
from sellershield_agent.store import get_store_client
from sellershield_agent.execution.common import build_service_ctx
from sellershield_agent.clock import SystemClock

async def main():
    get_settings.cache_clear()
    settings = get_settings()
    print("STORE_MODE:", settings.STORE_MODE)
    print("STORE_EXECUTOR_CLIENT_ID:", settings.STORE_EXECUTOR_CLIENT_ID)
    client = get_store_client(SystemClock(), settings)
    ctx = build_service_ctx(SystemClock(), "req-test-1")
    try:
        dt = await client.sync_clock()
        print("sync_clock ok:", dt)
    except Exception as e:
        print("sync_clock failed:", type(e), e)

    try:
        # Check transition_return
        res = await client.transition_return(ctx, "RET-2032", "approved", "Policy auto-approved", "key-test-123")
        print("transition_return result:", res)
    except Exception as e:
        print("transition_return failed:", type(e), e)

asyncio.run(main())
