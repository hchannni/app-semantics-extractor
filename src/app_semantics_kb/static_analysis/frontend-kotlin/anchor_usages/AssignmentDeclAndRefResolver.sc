import io.shiftleft.codepropertygraph.Cpg

@main def assignmentDeclAndRefResolver(
  cpgPath: String,
  viewAnchorsPath: String,
  outputPath: String = "assignment-declarations.json"
): Unit = {
  implicit val cpg: Cpg = AssignmentDeclRefInput.loadCpg(cpgPath)
  val viewAnchors = AssignmentDeclRefInput.parseViewAnchors(viewAnchorsPath)

  val assignmentAnchors = viewAnchors.filter(_.usageType == "ASSIGNMENT")
  println(s"[+] Loaded ${viewAnchors.size} anchors, ${assignmentAnchors.size} assignment anchors.")

  val payload = assignmentAnchors.flatMap(anchor => AssignmentDeclRefReport.assignmentPayload(anchor))
  AssignmentDeclRefReport.writeJson(outputPath, ujson.write(ujson.Arr(payload: _*), indent = 2))

  println(s"[+] Saved assignment decl & refs to $outputPath")
}

// ./joern \
//   --import /path/to/joern/view_anchors/ViewAnchorContract.sc \
//   --import /path/to/joern/anchor_usages/decl_ref_resolver/AssignmentDeclRefInput.sc \
//   --import /path/to/joern/anchor_usages/decl_ref_resolver/AssignmentDeclRefCore.sc \
//   --import /path/to/joern/anchor_usages/decl_ref_resolver/AssignmentDeclRefExpansion.sc \
//   --import /path/to/joern/anchor_usages/decl_ref_resolver/AssignmentDeclRefReport.sc \
//   --script /path/to/joern/anchor_usages/AssignmentDeclAndRefResolver.sc \
//   --param cpgPath=/path/to/joern/joern-cli/alarmclock.cpg \
//   --param viewAnchorsPath=/path/to/joern/view-anchors.json \
//   --param outputPath=/path/to/joern/assignment-declarations.json
