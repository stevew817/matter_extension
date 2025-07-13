from modules.parameters import Types, Formats, ID
import modules.channel as _base
import modules.util as _util
import time
import pyocd
from pyocd.core.helpers import ConnectHelper
from pyocd.core.memory_map import MemoryMap, MemoryRegion, MemoryType
from pyocd.core.soc_target import SoCTarget
from ctypes import Structure, c_char, c_int32, c_uint32, sizeof
# https://github.com/pyocd/pyOCD
# pip install pyocd==0.34.3 (later versions have issue with EFR32MG24 flashloader from pack)

class SEGGER_RTT_BUFFER_UP(Structure):
    """@brief `SEGGER RTT Ring Buffer` target to host."""

    _fields_ = [
        ("sName", c_uint32),
        ("pBuffer", c_uint32),
        ("SizeOfBuffer", c_uint32),
        ("WrOff", c_uint32),
        ("RdOff", c_uint32),
        ("Flags", c_uint32),
    ]


class SEGGER_RTT_BUFFER_DOWN(Structure):
    """@brief `SEGGER RTT Ring Buffer` host to target."""

    _fields_ = [
        ("sName", c_uint32),
        ("pBuffer", c_uint32),
        ("SizeOfBuffer", c_uint32),
        ("WrOff", c_uint32),
        ("RdOff", c_uint32),
        ("Flags", c_uint32),
    ]


class SEGGER_RTT_CB(Structure):
    """@brief `SEGGER RTT control block` structure. """

    _fields_ = [
        ("acID", c_char * 16),
        ("MaxNumUpBuffers", c_int32),
        ("MaxNumDownBuffers", c_int32),
        ("aUp", SEGGER_RTT_BUFFER_UP * 3),
        ("aDown", SEGGER_RTT_BUFFER_DOWN * 3),
    ]


class PyOCDChannel(_base.Channel):

    def __init__(self, paths, args, conn, comm) -> None:
        super().__init__(_base.Channel.PyOCD)
        self.session = pyocd.core.session.Session(comm.probe['probe'], options=comm.options)
        self._sent_finish = False

    def is_open(self):
        return self.session.is_open

    def open(self):
        try:
            self.session.open()
        except Exception as e:
            print("Caught exception {}".format(e))

        try:
            target: SoCTarget = self.session.board.target

            memory_map: MemoryMap = target.get_memory_map()
            ram_region: MemoryRegion = memory_map.get_default_region_of_type(MemoryType.RAM)

            rtt_range_start = ram_region.start
            rtt_range_size = ram_region.length

            print(f"RTT control block search range [{rtt_range_start:#08x}, {rtt_range_size:#08x}]")

            self._rtt_cb_addr = -1
            data = bytearray(b'0000000000')
            chunk_size = 1024
            while rtt_range_size > 0:
                read_size = min(chunk_size, rtt_range_size)
                data += bytearray(target.read_memory_block8(rtt_range_start, read_size))
                pos = data[-(read_size + 10):].find(b"SEGGER RTT")
                if pos != -1:
                    self._rtt_cb_addr = rtt_range_start + pos - 10
                    break
                rtt_range_start += read_size
                rtt_range_size -= read_size

            if self._rtt_cb_addr == -1:
                print("No RTT control block available")
                return 1

            data = target.read_memory_block8(self._rtt_cb_addr, sizeof(SEGGER_RTT_CB))
            self._rtt_cb = SEGGER_RTT_CB.from_buffer(bytearray(data))
            self._up_addr = self._rtt_cb_addr + SEGGER_RTT_CB.aUp.offset
            self._down_addr = self._up_addr + sizeof(SEGGER_RTT_BUFFER_UP) * self._rtt_cb.MaxNumUpBuffers

            print(f"_SEGGER_RTT @ {self._rtt_cb_addr:#08x} with {self._rtt_cb.MaxNumUpBuffers} aUp and {self._rtt_cb.MaxNumDownBuffers} aDown")

            # some targets might need this here
            #target.reset_and_halt()

            target.resume()

        except Exception:
            self.close()
            raise

    def close(self):
       print("* Connection closed.\n")
       self.session.close()


    def write(self, data : bytearray):
        # SEND TO TARGET
        if not self.is_open():
            self.open()

        print(f"Sending {data}")
        if data[:2] == bytearray(b'\x02\x02'):
            self._sent_finish = True
            self._sent_finish_cmd = data

        downblock = self.session.target.read_memory_block8(self._down_addr, sizeof(SEGGER_RTT_BUFFER_DOWN))
        down = SEGGER_RTT_BUFFER_DOWN.from_buffer(bytearray(downblock))

        sent = 0
        while len(data) > 0:
            try:
                # compute free space in down buffer
                if down.WrOff >= down.RdOff:
                    num_avail = down.SizeOfBuffer - (down.WrOff - down.RdOff)
                else:
                    num_avail = down.RdOff - down.WrOff - 1

                # write what we can
                bytes_to_write = min(num_avail, len(data))
                data_to_write = data[:num_avail]
                data = data[num_avail:]

                # write data to down buffer (host -> target), char by char
                for i in range(len(data_to_write)):
                    self.session.target.write_memory_block8(down.pBuffer + down.WrOff, data_to_write[i:i+1])
                    down.WrOff += 1
                    if down.WrOff == down.SizeOfBuffer:
                        down.WrOff = 0

                    sent += 1
                self.session.target.write_memory(self._down_addr + SEGGER_RTT_BUFFER_DOWN.WrOff.offset, down.WrOff)
            except Exception:
                # Probably the result of a reset, give the target some time and close the session
                self._sent_finish = False
                self.close()
                time.sleep(1)
                break

        return sent
        # byte array to send via RTT
        cmd = bytes()

        while True:
            # read data from up buffers (target -> host)
            data = target.read_memory_block8(up_addr, sizeof(SEGGER_RTT_BUFFER_UP))
            up = SEGGER_RTT_BUFFER_UP.from_buffer(bytearray(data))

            if up.WrOff > up.RdOff:
                """
                |oooooo|xxxxxxxxxxxx|oooooo|
                0    rdOff        WrOff    SizeOfBuffer
                """
                data = target.read_memory_block8(up.pBuffer + up.RdOff, up.WrOff - up.RdOff)
                target.write_memory(up_addr + SEGGER_RTT_BUFFER_UP.RdOff.offset, up.WrOff)
                print(bytes(data).decode(), end="", flush=True)

            elif up.WrOff < up.RdOff:
                """
                |xxxxxx|oooooooooooo|xxxxxx|
                0    WrOff        RdOff    SizeOfBuffer
                """
                data = target.read_memory_block8(up.pBuffer + up.RdOff, up.SizeOfBuffer - up.RdOff)
                data += target.read_memory_block8(up.pBuffer, up.WrOff)
                target.write_memory(up_addr + SEGGER_RTT_BUFFER_UP.RdOff.offset, up.WrOff)
                print(bytes(data).decode(), end="", flush=True)

            else: # up buffer is empty

                # try and fetch character
                if not kb.kbhit():
                    continue
                c = kb.getch()

                if ord(c) == 8 or ord(c) == 127: # process backspace
                    print("\b \b", end="", flush=True)
                    cmd = cmd[:-1]
                    continue
                elif ord(c) == 27: # process ESC
                    break
                else:
                    print(c, end="", flush=True)
                    cmd += c.encode()

                # keep accumulating until we see CR or LF
                if not c in "\r\n":
                    continue

                # SEND TO TARGET

                data = target.read_memory_block8(down_addr, sizeof(SEGGER_RTT_BUFFER_DOWN))
                down = SEGGER_RTT_BUFFER_DOWN.from_buffer(bytearray(data))

                # compute free space in down buffer
                if down.WrOff >= down.RdOff:
                    num_avail = down.SizeOfBuffer - (down.WrOff - down.RdOff)
                else:
                    num_avail = down.RdOff - down.WrOff - 1

                # wait until there's space for the entire string in the RTT down buffer
                if (num_avail < len(cmd)):
                    continue

                # write data to down buffer (host -> target), char by char
                for i in range(len(cmd)):
                    target.write_memory_block8(down.pBuffer + down.WrOff, cmd[i:i+1])
                    down.WrOff += 1
                    if down.WrOff == down.SizeOfBuffer:
                        down.WrOff = 0;
                target.write_memory(down_addr + SEGGER_RTT_BUFFER_DOWN.WrOff.offset, down.WrOff)

                # clear it and start anew
                cmd = bytes()

    def read(self):
        # read data from up buffers (target -> host)
        if not self.is_open():
            self.open()

        # Do a blocking read: wait for the first byte to appear, then read until no more data appears
        data_recv = bytearray()
        data_read = bytearray()

        while (len(data_read) > 0) or (0 == len(data_recv)):
            try:
                data_read = bytearray()
                up_buf = self.session.target.read_memory_block8(self._up_addr, sizeof(SEGGER_RTT_BUFFER_UP))
                up = SEGGER_RTT_BUFFER_UP.from_buffer(bytearray(up_buf))

                if up.WrOff > up.RdOff:
                    """
                    |oooooo|xxxxxxxxxxxx|oooooo|
                    0    rdOff        WrOff    SizeOfBuffer
                    """
                    data_read = self.session.target.read_memory_block8(up.pBuffer + up.RdOff, up.WrOff - up.RdOff)
                    self.session.target.write_memory(self._up_addr + SEGGER_RTT_BUFFER_UP.RdOff.offset, up.WrOff)

                elif up.WrOff < up.RdOff:
                    """
                    |xxxxxx|oooooooooooo|xxxxxx|
                    0    WrOff        RdOff    SizeOfBuffer
                    """
                    data_read = self.session.target.read_memory_block8(up.pBuffer + up.RdOff, up.SizeOfBuffer - up.RdOff)
                    data_read += self.session.target.read_memory_block8(up.pBuffer, up.WrOff)
                    self.session.target.write_memory(self._up_addr + SEGGER_RTT_BUFFER_UP.RdOff.offset, up.WrOff)
                else:
                    # Buffer offsets are equal, no data to read
                    data_read = bytearray()

            except Exception:
                # Probably the result of a reset, give the target some time and close the session
                self.close()
                time.sleep(0.5)

                # Catching this exception can also happen after the target reset itself in response to the finish command.
                # Emulate the correct response here.
                if self._sent_finish and len(data_recv) == 0:

                    if self._sent_finish_cmd[0] == 0x02:
                        data_read = self._sent_finish_cmd[:3]
                        # Add response flag and OK code
                        data_read[1] = data_read[1] | 0x80
                        data_read.extend(b'\x00\x00\x00\x00\x00')
                        data_read.extend(b'\x00\x00')

                break

            finally:
                data_recv.extend(data_read)

        print(f"Received {data_recv}")
        return bytes(data_recv)


    def reset(self, do_halt = False):
        with self.session:
            self.session.target.reset(reset_type=pyocd.core.target.Target.ResetType.HW)


    def flash(self, firmware_path, address):
        raise NotImplementedError
