import json
from pathlib import Path
import xml.etree.ElementTree as ET
import subprocess
import io


class A11yTreeParser:
    def __init__(
        self,
        *,
        drop_keys: set[str] | None = None,
        always_drop_keys: set[str] | None = None,
        always_keep_keys: set[str] | None = None,
        drop_empty_string: bool = True,
        drop_false: bool = True,
    ):
        self.drop_keys = drop_keys or set()
        self.always_drop_keys = always_drop_keys or {"package"}
        self.always_keep_keys = always_keep_keys or {"bounds"}
        self.drop_empty_string = drop_empty_string
        self.drop_false = drop_false

    def _coerce_value(self, value: str):
        v = value.strip()
        if v.lower() == "true":
            return True
        if v.lower() == "false":
            return False
        return v

    def _clean_attrib_inplace(self, attrib: dict) -> None:
        keys = list(attrib.keys())
        for k in keys:
            if k in self.always_drop_keys or k in self.drop_keys:
                attrib.pop(k, None)
                continue

            raw_v = attrib.get(k)
            if raw_v is None:
                attrib.pop(k, None)
                continue

            if k in self.always_keep_keys:
                continue

            v = self._coerce_value(str(raw_v))
            if self.drop_false and v is False:
                attrib.pop(k, None)
                continue
            if self.drop_empty_string and isinstance(v, str) and v == "":
                attrib.pop(k, None)
                continue

            if v is True:
                attrib[k] = "true"
            elif v is False:
                attrib.pop(k, None)
            else:
                attrib[k] = str(v)

    def _clean_attrib(self, attrib: dict) -> dict:
        out = {}

        for k in self.always_keep_keys:
            bounds_value = attrib.get(k)
            if bounds_value is not None:
                out[k] = bounds_value

        for k, raw_v in attrib.items():
            if k in self.always_drop_keys or k in self.drop_keys:
                continue
            if k in self.always_keep_keys:
                continue
            if raw_v is None:
                continue

            v = self._coerce_value(raw_v)
            if self.drop_false and v is False:
                continue
            if self.drop_empty_string and isinstance(v, str) and v == "":
                continue
            out[k] = v

        return out

    def _parse_node(self, elem: ET.Element) -> dict:
        node = self._clean_attrib(elem.attrib)
        children = []
        for child in list(elem):
            if child.tag != "node":
                continue
            children.append(self._parse_node(child))
        if children:
            node["children"] = children
        return node

    def parse(self, xml_path: str | Path) -> dict:
        xml_path = Path(xml_path)
        tree = ET.parse(xml_path)
        root = tree.getroot()

        out = self._clean_attrib(root.attrib)
        nodes = []
        for child in list(root):
            if child.tag != "node":
                continue
            nodes.append(self._parse_node(child))
        out["nodes"] = nodes
        return out

    def parse_xml(self, xml_text: str) -> dict:
        root = ET.fromstring(xml_text)

        out = self._clean_attrib(root.attrib)
        nodes = []
        for child in list(root):
            if child.tag != "node":
                continue
            nodes.append(self._parse_node(child))
        out["nodes"] = nodes
        return out

    def compact_xml(self, xml_text: str, *, pretty: bool = True) -> str:
        tree = ET.ElementTree(ET.fromstring(xml_text))
        root = tree.getroot()

        for elem in root.iter():
            self._clean_attrib_inplace(elem.attrib)

        if pretty:
            ET.indent(tree, space="  ", level=0)

        buf = io.BytesIO()
        tree.write(buf, encoding="utf-8", xml_declaration=True, short_empty_elements=True)
        return buf.getvalue().decode("utf-8")

    def dump_xml_from_adb(
        self,
        *,
        serial: str | None = None,
        remote_path: str = "/sdcard/window_dump.xml",
        delete_remote: bool = True,
    ) -> str:
        base = ["adb"]
        if serial:
            base += ["-s", serial]

        dump_cmd = base + ["shell", "uiautomator", "dump", remote_path]
        proc = subprocess.run(dump_cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"adb uiautomator dump failed: {proc.stderr.strip() or proc.stdout.strip()}")

        cat_cmd = base + ["exec-out", "cat", remote_path]
        proc2 = subprocess.run(cat_cmd, capture_output=True, text=True)
        if proc2.returncode != 0:
            raise RuntimeError(f"adb cat dump failed: {proc2.stderr.strip() or proc2.stdout.strip()}")

        xml_text = proc2.stdout
        if delete_remote:
            subprocess.run(base + ["shell", "rm", "-f", remote_path], capture_output=True, text=True)

        if not xml_text.strip().startswith("<?xml") and "<hierarchy" not in xml_text:
            raise RuntimeError("adb dump returned empty or non-XML output")

        return xml_text

    def parse_from_adb(self, *, serial: str | None = None) -> dict:
        xml_text = self.dump_xml_from_adb(serial=serial)
        return self.parse_xml(xml_text)

    def dump_to_file_from_adb(
        self,
        out_path: str | Path,
        *,
        serial: str | None = None,
        remote_path: str = "/sdcard/window_dump.xml",
        delete_remote: bool = True,
        pretty: bool = True,
    ) -> str:
        out_path = Path(out_path)
        xml_text = self.dump_xml_from_adb(
            serial=serial,
            remote_path=remote_path,
            delete_remote=delete_remote,
        )
        xml_text = self.compact_xml(xml_text, pretty=pretty)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(xml_text, encoding="utf-8")
        return xml_text


if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parent.parent
    default_xml = base_dir / "a11y_dump.xml"
    parsed = A11yTreeParser().parse(default_xml)
    print(json.dumps(parsed, ensure_ascii=False, indent=2))
