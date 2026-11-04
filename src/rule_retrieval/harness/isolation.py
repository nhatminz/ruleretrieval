"""Defense in depth for deployments without the provided OS network namespace."""
import ipaddress
import socket
import sys
from urllib.parse import urlsplit
from pathlib import Path


def deny_egress_except_inference(base_url:str,uds:str|None=None):
    parsed=urlsplit(base_url)
    if parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Inference endpoint must be a plain HTTP(S) origin without URL credentials")
    port=parsed.port or (443 if parsed.scheme=="https" else 80)
    allowed=set()
    if not uds:
        allowed={(r[4][0],port) for r in socket.getaddrinfo(parsed.hostname,port,type=socket.SOCK_STREAM)}
    socket_path=str(Path(uds).resolve()) if uds else None

    def audit(event,args):
        if event=="socket.getaddrinfo":
            host,requested_port=args[0],args[1]
            if uds or host!=parsed.hostname or int(requested_port)!=port:
                raise PermissionError("Runtime DNS egress denied")
        if event=="socket.connect":
            sock,address=args
            if sock.family==socket.AF_UNIX:
                if not socket_path or str(address)!=socket_path:
                    raise PermissionError("Unapproved Unix socket")
            elif uds or not isinstance(address,tuple) or (address[0],address[1]) not in allowed:
                raise PermissionError("Runtime network egress denied")
    sys.addaudithook(audit)
