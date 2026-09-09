"""Small HTTP CONNECT proxy for the internal browser network.

Only public internet addresses on ports 80/443 are reachable. DNS results are
validated and the connection uses the checked IP, preventing DNS rebinding.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


def public_address(value):
    ip = ipaddress.ip_address(value)
    if getattr(ip, 'ipv4_mapped', None):
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def resolve_public(host, port):
    if port not in {80, 443} or not host or len(host) > 253:
        raise ValueError('Destination denied.')
    answers = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses or any(not public_address(address) for address in addresses):
        raise ValueError('Destination denied.')
    return addresses[0]


async def handle(reader, writer):
    upstream = None
    try:
        header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 15)
        if len(header) > 32768:
            raise ValueError
        first = header.split(b'\r\n', 1)[0].decode('ascii')
        method, target, version = first.split(' ')
        if version not in {'HTTP/1.0', 'HTTP/1.1'}:
            raise ValueError
        if method == 'CONNECT':
            parsed = urlsplit('https://' + target)
            host, port = parsed.hostname, parsed.port or 443
            if parsed.path or parsed.username or parsed.password:
                raise ValueError
        elif method in {'GET', 'HEAD', 'POST'}:
            parsed = urlsplit(target)
            if parsed.scheme != 'http' or parsed.username or parsed.password:
                raise ValueError
            host, port = parsed.hostname, parsed.port or 80
        else:
            raise ValueError
        address = await resolve_public(host, port)
        remote_reader, upstream = await asyncio.wait_for(asyncio.open_connection(address, port), 20)
        if method == 'CONNECT':
            writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        else:
            # Close each plain HTTP connection after one request; a second
            # absolute URI cannot reuse this checked connection as a proxy hop.
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            lines = header.split(b'\r\n')[1:-2]
            lines = [line for line in lines if line.split(b':', 1)[0].lower() not in {b'connection', b'proxy-connection', b'proxy-authorization'}]
            upstream.write(f'{method} {path} HTTP/1.1\r\n'.encode() + b'\r\n'.join(lines) + b'\r\nConnection: close\r\n\r\n')
            await upstream.drain()
        await writer.drain()
        async def pipe(source, destination):
            while chunk := await source.read(65536):
                destination.write(chunk)
                await destination.drain()
        tasks = [asyncio.create_task(pipe(reader, upstream)), asyncio.create_task(pipe(remote_reader, writer))]
        _, pending = await asyncio.wait(tasks, timeout=1800, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    except Exception:
        try:
            writer.write(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
            await writer.drain()
        except Exception:
            pass
    finally:
        if upstream:
            upstream.close()
        writer.close()


async def main():
    server = await asyncio.start_server(handle, '0.0.0.0', 3128, limit=32768)
    async with server:
        await server.serve_forever()


if __name__ == '__main__':
    asyncio.run(main())
