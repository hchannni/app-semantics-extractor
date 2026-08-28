import os
import xml.etree.ElementTree as ET
import re
import json
from typing import List, Dict, Set, Optional
from pathlib import Path

class ViewModelMapper:
    """
    Android 코드베이스를 정적으로 분석하여 UI 요소(View)와 데이터 모델(Model) 간의 매핑 정보를 생성하는 클래스입니다.

    Checklist: 
    - UI 요소 파싱 (완)
    - 데이터 모델 불러오기 (완)
    - 컨트롤러에서 데이터 사용 흐름 파싱 (TODO)
    - UI 요소 - 컨트롤러에서 데이터 모델 프로퍼티와 매핑되는 관계 로직 파싱 (TODO)
    """

    def __init__(self, root_dir: str, data_model_schema_path: Optional[str] = None):
        """
        Args:
            root_dir: 'main' 폴더가 포함된 코드베이스의 루트 디렉토리 경로
            data_model_schema_path: DataModelParser가 생성한 JSON 스키마 파일 경로 (optional)
        """
        self.root_dir = root_dir
        self.layout_dir = os.path.join(root_dir, 'main', 'res', 'layout')
        self.java_dir = os.path.join(root_dir, 'main', 'java')
        
        # XML 파싱을 위한 Android 네임스페이스 정의
        self.android_ns = '{http://schemas.android.com/apk/res/android}'
        self.id_attr = f'{self.android_ns}id'
        
        # 데이터 모델 스키마 정보
        self.data_model_schemas = []
        if data_model_schema_path:
            self.load_data_model_schema(data_model_schema_path)

    def load_data_model_schema(self, schema_file_path: str) -> Dict:
        """
        DataModelParser가 생성한 JSON 스키마 파일을 로드합니다.
        
        Args:
            schema_file_path: JSON 스키마 파일 경로
        Returns:
            Dict: 로드된 스키마 데이터
        """
        if not os.path.exists(schema_file_path):
            raise FileNotFoundError(f"Data model schema file not found: {schema_file_path}")
        
        try:
            with open(schema_file_path, 'r', encoding='utf-8') as f:
                schema_data = json.load(f)
            
            self.data_model_schemas = schema_data.get('schemas', [])
            print(f"✅ Data model schema loaded: {len(self.data_model_schemas)} classes")
            print(f"   From: {schema_file_path}")
            
            # 로드된 클래스들 목록 출력
            class_names = [schema['name'] for schema in self.data_model_schemas]
            print(f"   Classes: {', '.join(class_names)}")
            
            return schema_data
            
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON format in schema file: {e}")
        except Exception as e:
            raise Exception(f"Error loading schema file: {e}")

    def find_latest_schema_file(self, schema_dir: Optional[str] = None) -> Optional[str]:
        """
        지정된 디렉토리에서 가장 최근에 생성된 data_model_schema_*.json 파일을 찾습니다.
        
        Args:
            schema_dir: 스키마 파일이 있는 디렉토리 (기본값: samples/SimpleAlarmClock)
        Returns:
            str: 가장 최근 스키마 파일 경로 또는 None
        """
        if schema_dir is None:
            # 기본 경로: samples/SimpleAlarmClock
            current_dir = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.dirname(current_dir)
            schema_dir = os.path.join(project_root, "samples", "SimpleAlarmClock")
        
        if not os.path.exists(schema_dir):
            print(f"⚠️ Schema directory not found: {schema_dir}")
            return None
        
        # data_model_schema_*.json 패턴의 파일들 찾기
        schema_files = []
        for filename in os.listdir(schema_dir):
            if filename.startswith('data_model_schema_') and filename.endswith('.json'):
                file_path = os.path.join(schema_dir, filename)
                schema_files.append(file_path)
        
        if not schema_files:
            print(f"⚠️ No data model schema files found in: {schema_dir}")
            return None
        
        # 파일 수정 시간 기준으로 가장 최근 파일 반환
        latest_file = max(schema_files, key=os.path.getmtime)
        print(f"📄 Found latest schema file: {os.path.basename(latest_file)}")
        return latest_file

    def auto_load_schema(self) -> bool:
        """
        자동으로 가장 최근 데이터 모델 스키마 파일을 찾아서 로드합니다.
        
        Returns:
            bool: 로드 성공 여부
        """
        latest_schema = self.find_latest_schema_file()
        if latest_schema:
            try:
                self.load_data_model_schema(latest_schema)
                return True
            except Exception as e:
                print(f"❌ Failed to load schema: {e}")
                return False
        return False

    def get_model_schema_by_name(self, class_name: str) -> Optional[Dict]:
        """
        클래스 이름으로 해당하는 데이터 모델 스키마를 찾습니다.
        
        Args:
            class_name: 찾고자 하는 클래스 이름
        Returns:
            Dict: 해당 클래스의 스키마 정보 또는 None
        """
        for schema in self.data_model_schemas:
            if schema['name'] == class_name:
                return schema
        return None

    def generate_mapping(self) -> Dict:
        """
        XML을 파싱하고, 정의된 data model schema를 가져와 View-Model Mapping 정보를 생성합니다.

        Returns:
            Dict: View-Model 매핑 정보
        """
        if not self.data_model_schemas:
            print("⚠️ No data model schemas loaded. Call load_data_model_schema() or auto_load_schema() first.")
            return {}
        
        # TODO: 실제 View-Model 매핑 로직 구현
        
        return {
            "ui_elements": [],
            "data_models": self.data_model_schemas,
            "mappings": []
        }

    def _find_layout_for_fragment(self, fragment_class_path: str) -> str | None:
        """
        Fragment 클래스 파일(`.kt`)의 내용을 분석하여, `onCreateView`에서 inflate하는 레이아웃 파일 이름을 찾습니다.

        Args: 
            fragment_class_path: Fragment 클래스 파일 경로
        Returns:
            str: R.layout.xxx 패턴에 의해 식별된 레이아웃 파일명
            None: 실패했을 경우
        """
        try:
            with open(fragment_class_path, 'r', encoding='utf-8') as f:
                content = f.read()

                # 정규식 -> "R.layout.xxx" 패턴 찾기
                match = re.search(r'R\.layout\.([a-zA-Z0-9_]+)', content)
                if match:
                    # e.g., "list_fragment" -> "list_fragment.xml"
                    return f"{match.group(1)}.xml"
        except (FileNotFoundError, UnicodeDecodeError) as e:
            print(f"⚠️  파일을 읽는 중 오류 발생: {fragment_class_path} ({e})")
            return None
        return None

    def _parse_xml_recursively(self, file_path: str, parsed_files: Set[str]) -> List[Dict]:
        """
        XML 레이아웃 파일을 재귀적으로 파싱합니다.
        (<include>와 <fragment> 태그를 만나면 해당 파일로 이동하여 재귀적으로 파싱)

        Args: 
            file_path: 현재 파싱하는 XML 레이아웃 파일 경로
            parsed_files: 지금까지 파싱한 XML 레이아웃 파일 집합 (재귀 도중 중복 파싱 방지)
        Returns:
            List[Dict]: 파싱 결과로 얻은 UI elements들의 리스트
        """
        # 이미 파싱했거나 파일이 존재하지 않는 경우 -> 중복 작업을 피하기 위해 return
        if file_path in parsed_files or not os.path.exists(file_path):
            return []

        parsed_files.add(file_path)
        ui_elements = []
        
        try:
            print(f"\n\n{os.path.basename(file_path)} 파일 파싱 시작")
            print(f"{'='*50}")
            tree = ET.parse(file_path)
            root = tree.getroot()

            # XML 파일 내의 모든 요소를 순회합니다.
            for element in root.iter():
                # 1. 'android:id' 속성을 가진 일반 UI 요소 추출
                if self.id_attr in element.attrib:
                    raw_id = element.attrib[self.id_attr]
                    view_id = raw_id.split('/')[-1]
                    view_type = element.tag  # e.g., "TextView", "EditText"
                    ui_elements.append({
                        "source_file": os.path.basename(file_path),
                        "view_id": view_id,
                        "view_type": view_type
                    })
                    print(f"UI Elements 추가: {view_id} (Type: {view_type})")

                # 2. <include> 태그를 만나면 재귀 호출
                if element.tag == 'include' and 'layout' in element.attrib:
                    included_layout_name = element.attrib['layout'].split('/')[-1]
                    included_file_path = os.path.join(self.layout_dir, f"{included_layout_name}.xml")
                    ui_elements.extend(self._parse_xml_recursively(included_file_path, parsed_files))
                
                # 3. <fragment> 태그를 만나면 클래스 파일을 분석하여 재귀 호출
                if element.tag == 'fragment' and f'{self.android_ns}name' in element.attrib:
                    fragment_class_name = element.attrib[f'{self.android_ns}name']
                    # Java/Kotlin 패키지 이름을 파일 경로로 변환
                    fragment_kt_path = os.path.join(self.java_dir, *fragment_class_name.split('.')) + '.kt'
                    
                    layout_file_name = self._find_layout_for_fragment(fragment_kt_path)
                    if layout_file_name:
                        fragment_layout_path = os.path.join(self.layout_dir, layout_file_name)
                        ui_elements.extend(self._parse_xml_recursively(fragment_layout_path, parsed_files))

        except ET.ParseError as e:
            print(f"❌ XML 파싱 오류 '{os.path.basename(file_path)}': {e}")
            
        return ui_elements

    def generate_mapping_for_activity(self, activity_class_name: str) -> List[Dict]:
        """
        분석을 시작할 진입점(Activity)을 지정하고, 연결된 모든 UI 요소를 추출합니다.
        """
        # 1단계: 컨트롤러(Activity/Fragment) 식별 및 컨텍스트 구축
        # TODO: 자동화 필요
        # E.g., AlarmsListActivity.kt -> setContentView(R.layout.list_activity)
        entry_layout_file = "list_activity.xml"
        
        # TODO: 자동화 필요
        related_fragment_layouts = [
            "list_fragment.xml",
            "list_row_bold.xml"
        ]
        
        entry_points = [os.path.join(self.layout_dir, f) for f in [entry_layout_file] + related_fragment_layouts]
        
        print(f"--- '{activity_class_name}' 분석 시작 ---")
        print(f"분석 진입점: {[os.path.basename(p) for p in entry_points]}")

        # 2단계: 범위가 지정된 UI 요소 추출 (재귀적으로)
        all_ui_elements = []
        parsed_files = set() 
        
        for path in entry_points:
            all_ui_elements.extend(self._parse_xml_recursively(path, parsed_files))
            
        # 중복 요소 제거
        # 딕셔너리는 hashable하지 않으므로, 튜플로 변환 후 set으로 중복을 제거하고 다시 딕셔너리로 만든다
        unique_elements = [dict(t) for t in {tuple(d.items()) for d in all_ui_elements}]
        
        # 파싱한 xml 파일명을 기준으로 정렬 -> 가독성 증가
        return sorted(unique_elements, key=lambda x: x['source_file'])


if __name__ == '__main__':
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    app_source_code_dir = os.path.join(project_root, 'samples', 'SimpleAlarmClock', 'app-source-code')
    
    mapper = ViewModelMapper(root_dir=os.path.join(app_source_code_dir, 'app', 'src'))
    
    # 데이터 모델 스키마 자동 로드
    if not mapper.auto_load_schema():
        print("⚠️ No data model schema found. Running DataModelParser first...")
        from data_model_parser import DataModelParser
        parser = DataModelParser()
        schema_file = parser.parse_and_save_all()
        mapper.load_data_model_schema(schema_file)
    
    # AlarmsListActivity와 관련된 모든 UI 요소 추출
    activity_ui_map = mapper.generate_mapping_for_activity("AlarmsListActivity")
    
    print(f"\n✅ 분석 완료: 총 {len(activity_ui_map)}개의 고유한 UI 요소를 찾았습니다.")
    
    # 최종 결과를 JSON 형식으로 출력
    print(json.dumps(activity_ui_map, indent=2, ensure_ascii=False))
    
    # 데이터 모델 정보도 출력
    print(f"\n📊 로드된 데이터 모델: {len(mapper.data_model_schemas)}개")
    for schema in mapper.data_model_schemas:
        print(f"  - {schema['name']}: {len(schema.get('properties', []))} properties")
