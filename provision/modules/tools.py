import os
import time
import shutil
import datetime
import base64
import hashlib
import struct
import pyocd
from pyocd.subcommands import pack_cmd
from pyocd.utility.cmdline import convert_reset_type
from pyocd.flash.file_programmer import FileProgrammer
import yaml
import bincopy
from ecdsa.curves import NIST256p
import modules.util as _util
from modules.parameters import ID
import argparse


class Commander:

    def __init__(self, args, conn):
        self.device = args.str(ID.kDevice)
        self.auto = ('auto' == args.str(ID.kAction))
        self.conn = conn

    def execute(self, args, output = True, check = True):
        args.insert(0, 'commander')
        if self.device is not None:
            args.extend(['--device', self.device])
        if self.conn is None:
            pass
        elif self.conn.serial_num:
            args.extend(['--serialno', self.conn.serial_num])
        elif self.conn.ip_addr:
            if self.conn.port:
                args.extend(['--ip', '{}:{}'.format(self.conn.ip_addr, self.conn.port)])
            else:
                args.extend(['--ip', self.conn.ip_addr])
        cmd = ' '.join(args)
        return _util.execute(args, output, check, retry = 2)

    def info(self):
        res = self.execute(['device', 'info'], True, False)
        if res is None: _util.fail("Cannot retrieve device info")
        return DeviceInfo(res)

    def flash(self, path):
        image_path = _util.Paths.quote(path)
        _, ext = os.path.splitext(path)
        if self.auto and ('si917' == self.device):
            self.execute(['manufacturing', 'erase', 'userdata'], False, True)
        if '.rps' == ext:
            self.execute(['rps', 'load', image_path], False, True)
            # Si917 needs time to start
            time.sleep(1)
        else:
            self.execute(['flash' , image_path], False, True)

    def reset(self):
        self.execute(['device', 'reset'], False, False)


class PyOCD:

    def __init__(self, args, conn):
        self.device = args.str(ID.kDevice)
        self.auto = ('auto' == args.str(ID.kAction))
        self.conn = conn

        # Auto-detect available adapters
        probes = pyocd.core.helpers.ConnectHelper.get_all_connected_probes()
        if len(probes) == 0:
            raise KeyError("No PyOCD adapters connected to this system")
        else:
            with open(os.path.join(os.path.dirname(__file__), 'pyocd_known_targets.yaml'), 'r') as file:
                known_adapters = yaml.safe_load(file)

            detected_probes = []
            for probe in probes:
                if conn.serial_num != "" and probe.unique_id != conn.serial_num:
                    continue

                # Check if the adapter is known to us
                for known_adapter in known_adapters['known_boards']:
                    if known_adapter['vendor_name'] == probe.vendor_name and known_adapter['product_name'] == probe.product_name:
                        # Add to list if autodetecting or match with both ID and DB
                        if conn.serial_num == "" or probe.unique_id == conn.serial_num:
                            detected_probes.append({'id': probe.unique_id, 'attr': known_adapter, 'probe': probe})
                    elif probe.unique_id == conn.serial_num and conn.address == known_adapter['shortname']:
                        # Add to list if matching ID and specific DB entry (escape hatch to use generic debug probes)
                        detected_probes.append({'id': probe.unique_id, 'attr': known_adapter, 'probe': probe})

            if len(detected_probes) == 0:
                raise KeyError("No known PyOCD adapters connected to this system")
            elif len(detected_probes) > 1:
                raise KeyError("More than 1 eligible PyOCD adapter detected - specify target using unique ID. Available IDs: {}".format([x['id'] for x in detected_probes]))
            else:
                self.probe = detected_probes[0]
                print("Using {} with ID {}".format(self.probe['attr']['shortname'], self.probe['id']))
        self.options = {
            # Some APs are regarded as nonconforming by PyOCD, so tell it to stick to AP0 on error
            'adi.v5.max_invalid_ap_count': 0,
            'target_override': self.probe['attr']['part'],
            'frequency': 8000000
        }

    def execute(self, args, output = True, check = True):
        args.insert(0, 'python -m pyocd')
        return _util.execute(args, output, check, retry = 2)

    def info(self):
        res = "Part Number: {}{}".format(self.probe['attr']['part'], os.linesep)
        res += "Flash Size: {} kb{}".format(self.probe['attr']['size'], os.linesep)

        return DeviceInfo(res.encode())

    def flash(self, path):
        try:
            session = pyocd.core.session.Session(self.probe['probe'], options=self.options)
        except pyocd.core.exceptions.TargetSupportError:
            print("Target support not found, trying to automatically install...")
            args = argparse.Namespace(
                update=True,
                patterns=["{}*".format(self.probe['attr']['part'][:9].upper())],
                verbose=0,
                quiet=0,
                clean=False,
                no_download=False
            )
            cmd = pack_cmd.PackInstallSubcommand(args)
            cmd.invoke()
            print("Retrying...")
            session = pyocd.core.session.Session(self.probe['probe'], options=self.options)

        converted = False
        if path[-4:] == ".s37":
            hexpath = path[:-4] + ".hex"
            if not os.path.exists(hexpath):
                # Need to convert srec to hex for PyOCD
                content = bincopy.BinFile(path)
                with open(hexpath, "w") as f:
                    f.write(content.as_ihex())
                converted = True
        else:
            hexpath = path

        try:
            with session:
                programmer = FileProgrammer(session)
                programmer.program(hexpath,
                                base_address=None,
                                skip=False,
                                file_format=None)
        finally:
            if converted:
                os.remove(hexpath)

        if '_ram' in path:
            # Flashed a ramloader, need to manually set PC/SP to execute
            content = bincopy.BinFile(path)
            start = content.minimum_address
            print("File starting at {}".format(hex(start)))

            start_sp = int.from_bytes(content[start: start+4], byteorder='little')
            start_pc = int.from_bytes(content[start+4: start+8], byteorder='little')

            with session:
                session.target.halt()
                cur_pc = session.target.read_core_register("pc")
                cur_sp = session.target.read_core_register("sp")
                print("Current SP/PC: 0x{} / 0x{}".format(hex(cur_sp), hex(cur_pc)))
                session.target.reset_and_halt(reset_type=pyocd.core.target.Target.ResetType.SW_SYSRESETREQ)
                session.target.write_core_register("sp", start_sp)
                session.target.write_core_register("pc", start_pc)
                session.target.resume()

        else:
            # Flashed a program to flash, just reset and let run
            with session:
                session.target.reset(reset_type=pyocd.core.target.Target.ResetType.HW)

        # Give target some time to come back
        time.sleep(1)

    def reset(self):
        session = pyocd.core.session.Session(self.probe['probe'], options=self.options)
        try:
            # Get the reset type from the session option.
            the_reset_type = convert_reset_type(session.options.get('reset_type'))

            # Handle hw reset more efficiently using the probe directly, so we don't need can skip
            # discovery. However, if halting was requested we need full init even if performing a
            # hardware reset.
            is_hw_reset = (the_reset_type == pyocd.core.target.Target.ResetType.HW) and not self._args.halt

            # Only init the board if performing a sw reset.
            session.open(init_board=(not is_hw_reset))
            assert session.probe
            assert session.target

            # If the reset type is default, get the concrete default type from the core so we can log it.
            if the_reset_type is None:
                session.target.selected_core = self._args.core

                # TODO This only works right now because all cores are CortexM. The default
                # reset type should really be moved to CoreTarget.
                the_reset_type = cast("CortexM", session.target.selected_core).default_reset_type

            print("Performing %s reset...", the_reset_type.name)
            if is_hw_reset:
                # For some probe types the probe still has to be connected to drive reset.
                session.probe.connect()
                session.probe.reset()
                session.probe.disconnect()
            else:
                if self._args.halt:
                    session.target.reset_and_halt(reset_type=the_reset_type)
                else:
                    session.target.reset(reset_type=the_reset_type)
            print("Done.")
        finally:
            session.close()

        time.sleep(1)


class DeviceInfo:

    def __init__(self, text):
        if text is None: _util.fail("Missing device info")
        d = self.parseLines(text.decode('utf-8').splitlines())
        self.part = self.parseField(d, 'Part Number')
        self.uid = self.parseField(d, 'Unique ID')
        self.revision = self.parseField(d, 'Die Revision')
        self.version = self.parseField(d, 'Production Ver')
        self.flash_size = self.parseSize(d, 'Flash Size')
        self.family = self.part[0:9].lower()

    def parseLines(self, lines):
        m = {}
        for l in lines:
            pair = l.split(':')
            if len(pair) > 1:
                m[pair[0].strip()] = pair[1].strip().lower()
        return m

    def parseField(self, d, tag, default_value = '?'):
        v =  tag in d and d[tag] or default_value
        return isinstance(v, str) and v.lower() or v

    def parseSize(self, d, tag):
        text = self.parseField(d, tag, '0')
        if text is None: return 0
        parts = text.split()
        value = int(parts[0])
        multiplier = 1
        if len(parts) > 0 and ('kb' == parts[1].lower()):
            multiplier = 1024
        return value * multiplier

    def __str__(self):
        text =  "{}+ part: '{}'\n".format(_util.MARGIN, self.part)
        text += "{}+ family: '{}'\n".format(_util.MARGIN, self.family)
        text += "{}+ version: '{}'\n".format(_util.MARGIN, self.version)
        text += "{}+ revision: '{}'\n".format(_util.MARGIN, self.revision)
        text += "{}+ flash_size: 0x{:08x}\n".format(_util.MARGIN, self.flash_size)
        return text


class CertTool:

    def __init__(self, tool_path, vid, pid):
        self.lifetime = 3660
        self.tool = tool_path
        self.vid = vid
        self.pid = pid

    def generateCD(self, cdc, cdk, cd):
        version = 0x101
        security_level = 0
        security_info = 0
        serial_num = self.generateSerial()
        # Remove existing CD
        if os.path.exists(cd):
            os.remove(cd)
        # Generate CD
        cdcq = _util.Paths.quote(cdc)
        cdkq = _util.Paths.quote(cdk)
        cdq = _util.Paths.quote(cd)
        self.execute(['gen-cd', '-f', '1', '-V', self.vid, '-p', self.pid, '-d', '0x0016', '-c', 'ZIG20142ZB330003-24', '-l', security_level, '-i', security_info, '-n', version, '-t', '0', '-o', self.vid, '-r' , self.pid, '-C', cdcq, '-K', cdkq, '-O', cdq ])

    def generatePAA(self, paa_cert, paa_key):
        # Remove existing PAA
        if os.path.exists(paa_cert):
            os.remove(paa_cert)
        if os.path.exists(paa_key):
            os.remove(paa_key)
        # Generate PAA
        paa_certq = _util.Paths.quote(paa_cert)
        paa_keyq = _util.Paths.quote(paa_key)
        self.execute(['gen-att-cert', '-t', 'a', '-l', self.lifetime, '-c', '"Matter PAA"', '-V', self.vid, '-o', paa_certq, '-O', paa_keyq])

    def generatePAI(self, paa_cert, paa_key, pai_cert, pai_key):
        # Remove existing PAI
        if os.path.exists(pai_cert):
            os.remove(pai_cert)
        if os.path.exists(pai_key):
            os.remove(pai_key)
        # Generate PAI
        paa_certq = _util.Paths.quote(paa_cert)
        paa_keyq = _util.Paths.quote(paa_key)
        pai_certq = _util.Paths.quote(pai_cert)
        pai_keyq = _util.Paths.quote(pai_key)
        self.execute(['gen-att-cert', '-t', 'i', '-l', self.lifetime, '-c', '"Matter PAI"', '-V', self.vid, '-P', self.pid, '-C', paa_certq, '-K', paa_keyq, '-o', pai_certq, '-O', pai_keyq])

    def generateDAC(self, pai_cert, pai_key, dac_cert, dac_key, common_name = 'Matter DAC'):
        # Remove existing DAC
        if os.path.exists(dac_cert):
            os.remove(dac_cert)
        if os.path.exists(dac_key):
            os.remove(dac_key)
        # Generate DAC
        cnq = '"{}"'.format(common_name)
        pai_certq = _util.Paths.quote(pai_cert)
        pai_keyq = _util.Paths.quote(pai_key)
        dac_certq = _util.Paths.quote(dac_cert)
        dac_keyq = _util.Paths.quote(dac_key)
        self.execute(['gen-att-cert', '-t', 'd', '-l', self.lifetime, '-c', cnq, '-V', self.vid, '-P', self.pid, '-C', pai_certq, '-K', pai_keyq, '-o', dac_certq, '-O', dac_keyq])

    def generateSerial(self):
        base_time = datetime.datetime(2000, 1, 1)
        delta = datetime.datetime.now() - base_time
        return delta.seconds

    def execute(self, args):
        if (self.tool is None) or (shutil.which(self.tool)) is None:
            raise ValueError("Missing Cert Tool");
        _util.execute([ self.tool ] + args)

class Spake2p:
    INVALID_PASSCODES = [00000000, 11111111, 22222222, 33333333, 44444444,
                            55555555, 66666666, 77777777, 88888888, 99999999, 12345678, 87654321]
    kSaltMin = 16
    kSaltMax = 32
    kIterationsMin = 1000
    kIterationsMax = 100000

    def __init__(self):
        pass

    @staticmethod
    def generateVerifier(passcode, iterations, salt_b64):
        if(passcode is None): _util.fail("Missing SPAKE2+ passcode")
        if(iterations is None): _util.fail("Missing SPAKE2+ iteration count")
        if(salt_b64 is None): _util.fail("Missing SPAKE2+ salt")

        salt = base64.b64decode(salt_b64)
        salt_length = len(salt)
        if salt_length < Spake2p.kSaltMin:
            fail("Invalid SPAKE2+ salt length: {} < {}".format(salt_length, Spake2p.kSaltMin))
        if salt_length > Spake2p.kSaltMax:
            fail("Invalid SPAKE2+ salt length: {} > {}".format(salt_length, Spake2p.kSaltMax))

        WS_LENGTH = NIST256p.baselen + 8
        ws = hashlib.pbkdf2_hmac('sha256', struct.pack('<I', passcode), salt, iterations, WS_LENGTH * 2)
        w0 = int.from_bytes(ws[:WS_LENGTH], byteorder='big') % NIST256p.order
        w1 = int.from_bytes(ws[WS_LENGTH:], byteorder='big') % NIST256p.order
        L = NIST256p.generator * w1
        verifier = w0.to_bytes(NIST256p.baselen, byteorder='big') + L.to_bytes('uncompressed')
        verifier_b64 = base64.b64encode(verifier).decode('utf-8')
        return verifier_b64


class QrCode(object):

    kVersionFieldLengthInBits = 3
    kVendorIDFieldLengthInBits = 16
    kProductIDFieldLengthInBits = 16
    kCommissioningFlowFieldLengthInBits = 2
    kRendezvousInfoFieldLengthInBits = 8
    kPayloadDiscriminatorFieldLengthInBits = 12
    kSetupPINCodeFieldLengthInBits = 27
    kPaddingFieldLengthInBits = 4

    @staticmethod
    def generateBits(args):
        # Vendor ID
        vendor_id = args.int(ID.kVendorId)
        if vendor_id is None: _util.fail("Missing verndor_id")
        # Product ID
        product_id = args.int(ID.kProductId)
        if product_id is None: _util.fail("Missing product_id")
        # Commissioning Flow
        commissioning_flow = args.int(ID.kCommissioningFlow)
        if commissioning_flow is None: _util.fail("Missing commissioning_flow")
        # Rendezvous Flags
        rendezvous_flags = args.int(ID.kRendezvousFlags)
        if rendezvous_flags is None: _util.fail("Missing rendezvous_flags")
        # Discriminator
        discriminator = args.int(ID.kDiscriminator)
        if discriminator is None: _util.fail("Missing discriminator")
        # SPAKE2+ passcode
        spake2p_passcode = args.int(ID.kSpake2pPasscode)
        if spake2p_passcode is None: _util.fail("Missing SPAKE2+ passcode")
        # Total payload size (in bits)
        total_payload_data_bits = (QrCode.kVersionFieldLengthInBits +
                                   QrCode.kVendorIDFieldLengthInBits +
                                   QrCode.kProductIDFieldLengthInBits +
                                   QrCode.kCommissioningFlowFieldLengthInBits +
                                   QrCode.kRendezvousInfoFieldLengthInBits +
                                   QrCode.kPayloadDiscriminatorFieldLengthInBits +
                                   QrCode.kSetupPINCodeFieldLengthInBits +
                                   QrCode.kPaddingFieldLengthInBits)

        offset = 0
        bits = [0] * int(total_payload_data_bits / 8)
        offset = QrCode.writeBits(bits, offset, 0, QrCode.kVersionFieldLengthInBits, total_payload_data_bits)
        offset = QrCode.writeBits(bits, offset, vendor_id, QrCode.kVendorIDFieldLengthInBits, total_payload_data_bits)
        offset = QrCode.writeBits(bits, offset, product_id, QrCode.kProductIDFieldLengthInBits, total_payload_data_bits)
        offset = QrCode.writeBits(bits, offset, commissioning_flow,
                                  QrCode.kCommissioningFlowFieldLengthInBits, total_payload_data_bits)
        offset = QrCode.writeBits(bits, offset, rendezvous_flags,
                                  QrCode.kRendezvousInfoFieldLengthInBits, total_payload_data_bits)
        offset = QrCode.writeBits(bits, offset, discriminator,
                                  QrCode.kPayloadDiscriminatorFieldLengthInBits, total_payload_data_bits)
        offset = QrCode.writeBits(bits, offset, spake2p_passcode, QrCode.kSetupPINCodeFieldLengthInBits, total_payload_data_bits)
        offset = QrCode.writeBits(bits, offset, 0, QrCode.kPaddingFieldLengthInBits, total_payload_data_bits)

        return bytes(bits)


    # Populates numberOfBits starting from LSB of input into bits, which is assumed to be zero-initialized
    @staticmethod
    def writeBits(bits, offset, input, bit_size, total_payload_bits):
        if ((offset + bit_size) > total_payload_bits):
            _util.fail("Invalid QR code bits: {} > {}".format(offset + bit_size, total_payload_bits))
            return

        index = offset
        offset += bit_size
        while (input != 0):
            if (input & 1):
                bits[int(index / 8)] |= (1 << (index % 8))
            index += 1
            input >>= 1

        return offset
