"""Own Waitress listeners, channels and worker drain alongside the app runtime."""
import time


class ManagedHTTPServer:
    def __init__(self, application, listener, *, threads=128):
        from waitress import create_server
        self.channels = {}
        self.server = create_server(application, sockets=[listener], threads=threads,
                                    map=self.channels, asyncore_loop_timeout=0.2)

    def run(self):
        self.server.run()

    def stop(self, timeout=3):
        # Called on the serving thread after run() unwinds. Closing a channel
        # does not cancel its Python worker; keep ownership unless it exits.
        deadline = time.monotonic() + max(0, timeout)
        safe = True
        for channel in list(self.channels.values()):
            try:
                if hasattr(channel, "outbuf_lock"):
                    # HTTPChannel.handle_close also wakes a worker waiting on
                    # output backpressure; closing only its socket cannot do so.
                    channel.handle_close()
                else:
                    channel.close()
            except Exception:
                safe = False
        try:
            self.server.task_dispatcher.shutdown(timeout=max(0, deadline - time.monotonic()))
        except Exception:
            safe = False
        return safe and not self.channels and not self.server.task_dispatcher.threads
