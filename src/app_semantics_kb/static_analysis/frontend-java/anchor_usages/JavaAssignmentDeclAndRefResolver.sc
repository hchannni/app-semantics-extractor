import io.shiftleft.codepropertygraph.Cpg

@main def javaAssignmentDeclAndRefResolver(
  cpgPath: String,
  viewAnchorsPath: String,
  outputPath: String = "assignment-declarations.json",
  runsDir: String = ""
): Unit = {
  implicit val cpg: Cpg = JavaAssignmentDeclRefInput.loadCpg(cpgPath)
  val viewAnchors = JavaAssignmentDeclRefInput.parseViewAnchors(viewAnchorsPath)
  val assignmentAnchors = viewAnchors.filter(_.usageType == "ASSIGNMENT")
  println(s"[+] Loaded ${viewAnchors.size} Java anchors, ${assignmentAnchors.size} assignment anchors.")

  val payload = assignmentAnchors.flatMap(anchor => JavaAssignmentDeclRefReport.assignmentPayload(anchor))
  JavaAssignmentDeclRefReport.writeJson(outputPath, runsDir, ujson.write(ujson.Arr(payload: _*), indent = 2))
  println(s"[+] Saved Java assignment decl & refs to $outputPath")
}
