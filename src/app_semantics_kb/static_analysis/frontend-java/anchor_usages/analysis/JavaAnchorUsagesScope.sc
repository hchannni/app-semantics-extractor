import io.shiftleft.codepropertygraph.Cpg
import io.shiftleft.codepropertygraph.generated.nodes.*
import io.shiftleft.semanticcpg.language.*
import io.shiftleft.semanticcpg.language.locationCreator
import io.shiftleft.semanticcpg.language.LazyLocation.apply

import scala.util.Try

object JavaAnchorUsagesScope {
  def loadNode(nodeId: Long)(implicit cpg: Cpg): Option[AstNode] =
    Try(cpg.graph.node(nodeId)).toOption.collect { case node: AstNode => node }

  def locationOf(node: AstNode): JavaAnchorUsagesModel.SourceLocation = {
    val file = Option(node.filename).getOrElse("?")
    val line = node.lineNumber.getOrElse(-1)
    JavaAnchorUsagesModel.SourceLocation(file, line)
  }

  def enclosingMethodFullName(node: AstNode)(implicit cpg: Cpg): String =
    node.start
      .repeat(_.astParent)(_.emit)
      .collectAll[Method]
      .headOption
      .flatMap(method => Option(method.fullName))
      .getOrElse("")

  def nearestCall(node: AstNode): Option[Call] =
    node match {
      case call: Call => Some(call)
      case _ =>
        node.start.repeat(_.astParent)(_.emit).collectAll[Call].headOption
    }

  def parentCalls(node: AstNode): List[Call] =
    node.start.repeat(_.astParent)(_.emit).collectAll[Call].l

  def ancestorsOfType[T](node: AstNode)(implicit tag: scala.reflect.ClassTag[T]): Iterator[T] =
    node.start.repeat(_.astParent)(_.emit).collectAll[T]

  def firstAncestorOfType[T](node: AstNode)(implicit tag: scala.reflect.ClassTag[T]): Option[T] =
    ancestorsOfType[T](node).headOption

  def subtreeContains(root: AstNode, targetId: Long): Boolean =
    root.id == targetId || root.ast.collect { case node: AstNode => node }.id.l.contains(targetId)

  def callerCallSitesOf(node: AstNode)(implicit cpg: Cpg): List[AstNode] =
    node.start
      .repeat(_.astParent)(_.emit)
      .collectAll[Method]
      .headOption
      .toList
      .flatMap { method =>
        Option(method.fullName).toList.flatMap { methodFullName =>
          cpg.call
            .methodFullNameExact(methodFullName)
            .l
            .collect { case call: Call => call: AstNode }
        }
      }

  private def directCalleeMethods(call: Call)(implicit cpg: Cpg): List[Method] = {
    val byFullName = Option(call.methodFullName)
      .toList
      .flatMap(fullName => cpg.method.fullNameExact(fullName).l)

    val bySameTypeName = Option(call.method).toList.flatMap { callerMethod =>
      callerMethod.typeDecl.headOption.toList.flatMap { typeDecl =>
        typeDecl.method.nameExact(call.name).l
      }
    }

    val bySameFileName = Option(call.file.name.headOption.getOrElse("")).filter(_.nonEmpty).toList.flatMap { file =>
      cpg.method
        .nameExact(call.name)
        .filter(method => method.file.name.headOption.contains(file))
        .l
    }

    (byFullName ++ bySameTypeName ++ bySameFileName)
      .filterNot(_.id == call.method.id)
      .distinctBy(_.id)
  }

  private def parameterReferenceNodes(
    method: Method,
    parameter: MethodParameterIn
  )(implicit cpg: Cpg): List[AstNode] = {
    val paramName = Option(parameter.name).getOrElse("")
    if (paramName.isEmpty) return Nil

    val directRefs =
      parameter.start._refIn.collect { case node: AstNode => node }.l

    val localRefs =
      method.ast
        .collectAll[Identifier]
        .nameExact(paramName)
        .filterNot(_.id == parameter.id)
        .l

    val lambdaRefs = {
      val lambdaFullNames = method.ast.collectAll[MethodRef].map(_.methodFullName).l.distinct
      lambdaFullNames.flatMap { fullName =>
        cpg.method
          .fullNameExact(fullName)
          .ast
          .collectAll[Identifier]
          .nameExact(paramName)
          .filterNot(_.id == parameter.id)
          .l
      }
    }

    val closureRefs =
      cpg.identifier
        .nameExact(paramName)
        .filterNot(_.id == parameter.id)
        .filter { id =>
          id._capturedByIn
            .collectAll[ClosureBinding]
            .flatMap(_._refOut.collectAll[Declaration])
            .id
            .contains(parameter.id)
        }
        .collect { case node: AstNode => node }
        .l

    (directRefs ++ localRefs ++ lambdaRefs ++ closureRefs).distinctBy(_.id)
  }

  def interProceduralArgumentTargets(anchorRef: AstNode, call: Call)(implicit cpg: Cpg): List[AstNode] = {
    val argumentIndexes =
      call.argument
        .collect {
          case arg: Expression
              if arg.argumentIndex > 0 && subtreeContains(arg, anchorRef.id) =>
            arg.argumentIndex
        }
        .l
        .distinct

    if (argumentIndexes.isEmpty) return Nil

    directCalleeMethods(call)
      .flatMap { callee =>
        argumentIndexes.flatMap { argumentIndex =>
          callee.parameter
            .filter(_.index == argumentIndex)
            .l
            .flatMap(parameter => parameterReferenceNodes(callee, parameter))
        }
      }
      .distinctBy(_.id)
  }
}
