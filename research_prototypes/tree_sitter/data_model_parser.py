from typing import List, Dict
import os
import re
import json
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv
from openai import OpenAI
from openai.types.chat import ChatCompletion
from tqdm import tqdm

class DataModelParser:
    def __init__(self):
        self.current_file_dir = os.path.dirname(os.path.abspath(__file__))
        self.project_root = os.path.dirname(self.current_file_dir)
        self.samples_source_code_path = os.path.join(self.project_root, "samples", "SimpleAlarmClock", "app-source-code")
        self.target_files = []
        self.data_model_schemas = []

        env_path = Path(__file__).parent / ".env"
        load_dotenv(env_path)
        self.api_key = os.getenv("OPENAI_API_KEY")
        if not self.api_key:
            print("⚠️ WARNING: OPENAI_API_KEY environment variable is not set.")


    def find_data_model_files(self, root_dir: str = None) -> List:
        """
        Data model schema 구성을 위해, 프로젝트에서 정의하는 data class의 종류를 모두 식별합니다.

        Args:
            root_dir: 프로젝트의 루트 디렉터리 경로
        Returns:
            List: target_files 데이터 모델 파일 리스트
        """
        if root_dir is None:
            root_dir = self.samples_source_code_path
        
        if not os.path.exists(root_dir):
            print(f"Source code path not found: {root_dir}")
            return []

        target_files = []
        
        # 1단계: 데이터 모델 파일 식별
        data_dir = os.path.join(root_dir, "app", "src", "main", "java", "com", "better", "alarm", "data")
        
        # 규칙 1 & 2: data class, enum class, sealed class 포함 파일
        for dirpath, _, filenames in os.walk(data_dir):
            for filename in filenames:
                if filename.endswith('.kt'):
                    file_path = os.path.join(dirpath, filename)
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        if 'data class' in content or 'enum class' in content or 'sealed class' in content:
                            target_files.append(file_path)
        
        # # Prefs.kt 파일 수동 추가 (자동화 불가능한가 ..?)
        # # 예상으로는, Settings 같은 Activity에서 Prefs를 활용한다는 로직을 찾거나 해야 할 듯하다.
        # prefs_file = os.path.join(data_dir, "Prefs.kt")
        # if prefs_file not in target_files:
        #     target_files.append(prefs_file)
        
        print(f"✅ Data model files: {len(target_files)} files found")
        print("\n".join([f" - {os.path.basename(f)}" for f in target_files]))   # 이런 문법 ..

        self.target_files = target_files
        return target_files

    def parse_kotlin_file(self, file_path: str) -> List:
        """
        Kotlin 파일을 파싱하여 클래스 이름과 속성 정보를 추출합니다.
        """
        schemas = []
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

            # 개선된 클래스 정의 블록을 찾는 정규식
            # sealed class, data object, companion object 등도 고려
            class_regex = re.compile(
                r'(?:@\w+\s+)*(?:data\s+|enum\s+|sealed\s+|abstract\s+)?(?:class)\s+([A-Za-z0-9_]+)(?:\s*\([^)]*\))?[^{]*\{([\s\S]*?)\n\}',
                re.MULTILINE
            )
            
            # 개선된 속성 정규식: getter, 여러 줄, 초기값 등을 고려
            # 1. 일반적인 val/var 속성
            prop_regex = re.compile(
                r'val\s+([a-zA-Z0-9_]+)\s*:\s*([a-zA-Z0-9_<>?.\s]+?)(?:\s*=[\s\S]*?)?(?=\n\s*(?:val|var|fun|class|object|\})|$)',
                re.MULTILINE
            )
            
            # 2. 커스텀 getter가 있는 속성
            getter_prop_regex = re.compile(
                r'val\s+([a-zA-Z0-9_]+)\s*\n\s*get\(\)\s*=',
                re.MULTILINE
            )

            for match in class_regex.finditer(content):
                class_name = match.group(1)
                class_body = match.group(2)
                
                properties = []
                
                # 일반적인 속성 찾기
                for prop_match in prop_regex.finditer(class_body):
                    prop_name = prop_match.group(1)
                    prop_type = prop_match.group(2).strip()
                    
                    # nullable 체크
                    is_nullable = '?' in prop_type
                    # ? 제거하고 타입만 추출
                    prop_type = prop_type.replace('?', '').strip()
                    
                    properties.append({
                        "name": prop_name,
                        "type": prop_type,
                        "description": "",
                        "isNullable": is_nullable
                    })
                
                # 커스텀 getter가 있는 속성 찾기
                for getter_match in getter_prop_regex.finditer(class_body):
                    prop_name = getter_match.group(1)
                    
                    # 이미 추가된 속성인지 확인
                    if not any(prop["name"] == prop_name for prop in properties):
                        # getter 로직을 분석해서 타입 추정 (간단한 경우만)
                        prop_type = "Boolean"  # 기본값, 실제로는 더 정교한 분석 필요
                        
                        properties.append({
                            "name": prop_name,
                            "type": prop_type,
                            "description": "",
                            "isNullable": False
                        })

                # sealed class의 경우 내부 object/class도 파싱
                if "sealed class" in content:
                    # sealed class 내부의 data object, data class 찾기
                    inner_class_regex = re.compile(
                        r'(?:data\s+)?(?:object|class)\s+([A-Za-z0-9_]+)(?:\s*\([^)]*\))?\s*:\s*' + class_name,
                        re.MULTILINE
                    )
                    
                    for inner_match in inner_class_regex.finditer(class_body):
                        inner_class_name = inner_match.group(1)
                        properties.append({
                            "name": inner_class_name.lower(),
                            "type": inner_class_name,
                            "description": f"Sealed class variant: {inner_class_name}",
                            "isNullable": False
                        })

                if properties:
                    schemas.append({
                        "name": class_name,
                        "description": "",
                        "properties": properties
                    })

        # # Prefs.kt 특수 케이스
        # if not schemas and "class Prefs" in content:
        #     class_name = "Prefs"
        #     # Prefs 클래스의 생성자 파라미터를 찾는다
        #     constructor_regex = re.compile(r'class\s+Prefs\s*private constructor\(([\s\S]*?)\)', re.MULTILINE)
        #     constructor_match = constructor_regex.search(content)
        #     if constructor_match:
        #         properties = []
        #         constructor_params = constructor_match.group(1)
        #         # 파라미터에서 속성을 추출
        #         for line in constructor_params.split(','):
        #             line = line.strip()
        #             if not line: continue
                    
        #             # 예: val snoozeDuration: RxDataStore<Int>
        #             param_match = re.search(r'val\s+([a-zA-Z0-9_]+)\s*:\s*(.+)', line)
        #             if param_match:
        #                 properties.append({
        #                     "name": param_match.group(1),
        #                     "type": param_match.group(2).strip(),
        #                     "description": "",
        #                     "isNullable": False     # 생성자 파라미터는 기본적으로 non-null
        #                 })
        #         if properties:
        #             schemas.append({
        #                 "name": class_name,
        #                 "description": "",
        #                 "properties": properties
        #             })

        return schemas

    def _create_llm_prompt(self, schema: Dict, source_code_path: str) -> str:
        """
        Data model schema 파싱한 정보를 바탕으로 LLM에게 전달할 프롬프트를 생성합니다.
        
        Args:
            schema: 단일 Data model schema 딕셔너리
            source_code_path: 데이터 모델이 포함되어 있는 소스코드 파일 경로
        Return:
            str: content를 포함하여 생성한 프롬프트
        """

        # LLM이 쉽게 파싱할 수 있도록 입력 스키마를 JSON 문자열로 변환
        schema_str = json.dumps(schema, indent=4, ensure_ascii=False)

        # 프롬프트에 포함시키기 위한 소스코드 불러오기
        with open(source_code_path, 'r', encoding='utf-8') as f:
            source_code = f.read()

        prompt = f"""
You are an expert in analyzing Android Java/Kotlin code.
The following is the data model schema information for an Android app.
Please analyze the role of the class and each property from a user's perspective.

After your analysis, you MUST respond by filling in the **class-level 'description'** and **each property-level 'description'** in concise and clear English, strictly following the 'Output Format' below.
You must return only the JSON object without any other explanations.

[Input Schema]
{schema_str}

[Output Format]
{{
  "name": "{schema['name']}",
  "description": "A concise description of the class.", <--- Fill here!
  "properties": [
    {{
      "name": "property_name_1",
      "type": "property_type_1",
      "description": "Description for the first property." <--- Fill here!
      ...
    }},
    {{
      "name": "property_name_2",
      "type": "property_type_1",
      "description": "Description for the second property." <--- Fill here!
      ...
    }}
  ]
}}

Also, reference this source code below while generating the description:
[Source Code]
{source_code}
"""
        return prompt.strip()

    def generate_descriptions_with_llm(self, schemas: List[Dict]) -> List[Dict]:
        """
        LLM에 query하여 각 schema와 property에 대한 간략한 description을 생성합니다.

        Args:
            schemas: 프로젝트 파일을 파싱해서 생성한 스키마 리스트
        Returns:
            List[Dict]: 스키마에 LLM이 생성한 descriptions를 추가한 완성본
        """
        if not self.api_key:
            print("⚠️ WARNING: The OPENAI_API_KEY environment variable is not set. The description generation will be skipped.")
            return schemas
        
        try:
            client = OpenAI(api_key=self.api_key)
        except ImportError:
            print("⚠️ Cannot find 'openai' library. The description generation will be skipped.")
            return schemas

        enriched_schemas = []
        
        # 스키마와 소스 파일 매핑을 위한 딕셔너리 생성
        schema_to_file_map = {}
        for file_path in self.target_files:
            file_schemas = self.parse_kotlin_file(file_path)
            for file_schema in file_schemas:
                schema_to_file_map[file_schema['name']] = file_path
        
        for schema in tqdm(schemas, desc="🤖 Generating 'descriptions'", unit="class"):
            try:
                # 해당 스키마의 소스 파일 경로 찾기
                source_file_path = schema_to_file_map.get(schema['name'])
                if not source_file_path:
                    print(f"⚠️ Source file not found for schema: {schema['name']}")
                    source_file_path = ""
                
                prompt = self._create_llm_prompt(schema, source_file_path)
                
                response: ChatCompletion = client.chat.completions.create(
                    model="gpt-4o",
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": "You are a useful auxiliary system that analyzes Android(Java/Kotlin) data models and outputs structured JSON."},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0
                )
                
                result = response.choices[0].message.content
                if not result:
                    schema["description"] = ""
                    enriched_schemas.append(schema)
                    continue
                    
                enriched_data = json.loads(result)
                
                # 생성된 설명을 원본 스키마에 병합
                schema["description"] = enriched_data.get("description", "")
                
                description_map = {prop["name"]: prop.get("description", "") for prop in enriched_data.get("properties", [])}
                
                for prop in schema["properties"]:
                    prop["description"] = description_map.get(prop["name"], "")

                enriched_schemas.append(schema)

            except Exception as e:
                print(f"❌ Error while processing '{schema["name"]}' schema: {e}")
                # 오류 발생 시 원본 스키마라도 결과에 포함
                enriched_schemas.append(schema)

        return enriched_schemas

    def save_data_model_schema(self, output_dir: str = None) -> str:
        """
        데이터 모델 스키마를 JSON 파일로 저장합니다.
        
        Args:
            output_dir: 저장할 디렉토리 경로 (기본값: samples/SimpleAlarmClock)
        Returns:
            str: 저장된 파일의 전체 경로
        """
        if output_dir is None:
            # samples/SimpleAlarmClock 폴더에 저장
            output_dir = os.path.join(self.project_root, "samples", "SimpleAlarmClock")
        
        # 출력 디렉토리가 없으면 생성
        os.makedirs(output_dir, exist_ok=True)
        
        # 파일명에 타임스탬프 추가
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = os.path.join(output_dir, f"data_model_schema_{timestamp}.json")
        
        # 스키마 데이터 구성
        schema_data = {
            "metadata": {
                "generated_at": datetime.now().isoformat(),
                "source_path": self.samples_source_code_path,
                "total_classes": len(self.data_model_schemas)
            },
            "schemas": self.data_model_schemas
        }
        
        # JSON 파일로 저장
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(schema_data, f, indent=4, ensure_ascii=False)
        
        print(f"✅ Data model schema saved to: {output_file}")
        return output_file

    def parse_and_save_all(self) -> str:
        """
        전체 파이프라인을 실행: 파일 찾기 -> 파싱 -> 저장
        
        Returns:
            str: 저장된 파일 경로
        """
        # 1. 데이터 모델 파일들 찾기
        data_model_files = self.find_data_model_files()
        
        # 2. 각 파일 파싱하여 스키마 추출
        all_schemas = []
        for file_path in data_model_files:
            file_schemas = self.parse_kotlin_file(file_path)
            all_schemas.extend(file_schemas)
            print(f"📄 Parsed {os.path.basename(file_path)}: {len(file_schemas)} classes found")
        
        # 3. 스키마 저장
        self.data_model_schemas = all_schemas

        # 4. description 생성 GPT query 수행
        self.generate_descriptions_with_llm(self.data_model_schemas)
        
        # 5. 파일로 저장
        output_file = self.save_data_model_schema()
        
        print(f"\n🎉 Total {len(all_schemas)} data model classes parsed and saved!")
        return output_file

    def load_data_model_schema(self, file_path: str) -> Dict:
        """
        저장된 데이터 모델 스키마 파일을 로드합니다.
        
        Args:
            file_path: JSON 스키마 파일 경로
        Returns:
            dict: 로드된 스키마 데이터
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Schema file not found: {file_path}")
        
        with open(file_path, 'r', encoding='utf-8') as f:
            schema_data = json.load(f)
        
        self.data_model_schemas = schema_data.get('schemas', [])
        print(f"✅ Data model schema loaded from: {file_path}")
        print(f"   Found {len(self.data_model_schemas)} classes")
        
        return schema_data

if __name__ == "__main__":
    parser = DataModelParser()

    # 전체 파이프라인 실행
    output_file = parser.parse_and_save_all()
