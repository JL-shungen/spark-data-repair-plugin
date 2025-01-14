/*
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License.  You may obtain a copy of the License at
 *
 *    http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package org.apache.spark.api.python

import scala.collection.mutable.ArrayBuffer

import org.apache.spark.python.DenialConstraints
import org.apache.spark.sql._
import org.apache.spark.util.LoggingBasedOnLevel
import org.apache.spark.util.RepairUtils._
import org.apache.spark.util.{Utils => SparkUtils}

object ErrorDetectorApi extends LoggingBasedOnLevel {

  def detectNullCells(qualifiedName: String, rowId: String, targetAttrList: String): DataFrame = {
    logBasedOnLevel(s"detectNullCells called with: qualifiedName=$qualifiedName " +
      s"rowId=$rowId targetAttrList=$targetAttrList")
    NullErrorDetector.detect(qualifiedName, rowId, SparkUtils.stringToSeq(targetAttrList))
  }

  def detectErrorCellsFromRegEx(
      qualifiedName: String,
      rowId: String,
      targetAttrList: String,
      attr: String,
      regex: String): DataFrame = {
    logBasedOnLevel(s"detectErrorCellsFromRegEx called with: qualifiedName=$qualifiedName " +
      s"rowId=$rowId targetAttrList=$targetAttrList regex=$regex")
    RegExErrorDetector.detect(qualifiedName, rowId, SparkUtils.stringToSeq(targetAttrList),
      Map("attr" -> attr, "regex" -> regex))
  }

  def detectErrorCellsFromConstraints(
      qualifiedName: String,
      rowId: String,
      targetAttrList: String,
      constraintFilePath: String,
      constraints: String): DataFrame = {
    logBasedOnLevel(s"detectErrorCellsFromConstraints called with: qualifiedName=$qualifiedName " +
      s"rowId=$rowId targetAttrlist=$targetAttrList constraintFilePath=$constraintFilePath")
    ConstraintErrorDetector.detect(qualifiedName, rowId, SparkUtils.stringToSeq(targetAttrList),
      Map("constraintFilePath" -> constraintFilePath, "constraints" -> constraints))
  }

  def detectErrorCellsFromOutliers(
      qualifiedName: String,
      rowId: String,
      continousAttrs: String,
      targetAttrList: String,
      approxEnabled: Boolean = false): DataFrame = {
    logBasedOnLevel(s"detectErrorCellsFromOutliers called with: qualifiedName=$qualifiedName " +
      s"rowId=$rowId targetAttrList=$targetAttrList approxEnabled=$approxEnabled")
    GaussianOutlierErrorDetector.detect(qualifiedName, rowId, SparkUtils.stringToSeq(targetAttrList),
      Map("continousAttrs" -> continousAttrs, "approxEnabled" -> approxEnabled))
  }
}

abstract class ErrorDetector extends RepairBase {

  def detect(
    qualifiedName: String,
    rowId: String,
    targetAttrs: Seq[String],
    options: Map[String, Any]): DataFrame

  protected def createEmptyResultDfFrom(df: DataFrame, rowId: String): DataFrame = {
    val rowIdType = df.schema.find(_.name == rowId).get.dataType.sql
    createEmptyTable(s"`$rowId` $rowIdType, attribute STRING")
  }

  protected def getOptionValue[T](key: String, options: Map[String, Any]): T = {
    assert(options.contains(key))
    options(key).asInstanceOf[T]
  }

  protected def loggingErrorStats(
      detectorIdent: String,
      inputName: String,
      errCellDf: DataFrame): Unit = {

    lazy val attrsToRepair = ArrayBuffer[String]()

    logBasedOnLevel({
      withTempView(errCellDf, "error_cells") { errCellView =>
        val errorNumOfEachAttribute = {
          val df = spark.sql(s"SELECT attribute, COUNT(1) FROM $errCellView GROUP BY attribute")
          df.collect.map { case Row(attribute: String, n: Long) =>
            attrsToRepair += attribute
            s"$attribute:$n"
          }
        }
        s"""
           |$detectorIdent found errors:
           |  ${errorNumOfEachAttribute.mkString("\n  ")}
         """.stripMargin
      }
    })
    logBasedOnLevel({
      val inputDf = spark.table(inputName)
      val tableAttrs = inputDf.schema.map(_.name)
      val tableAttrNum = tableAttrs.length
      val tableRowCnt = inputDf.count()
      val errCellNum = errCellDf.count()
      val totalCellNum = tableRowCnt * tableAttrNum
      val errRatio = (errCellNum + 0.0) / totalCellNum
      s"$detectorIdent found $errCellNum/$totalCellNum error cells (${errRatio * 100.0}%) of " +
        s"${attrsToRepair.size}/${tableAttrs.size} attributes (${attrsToRepair.mkString(",")}) " +
        s"in the input '$inputName'"
    })
  }
}

object NullErrorDetector extends ErrorDetector {

  override def detect(
      qualifiedName: String,
      rowId: String,
      targetAttrs: Seq[String],
      options: Map[String, Any] = Map.empty): DataFrame = {

    val inputDf = spark.table(qualifiedName)

    withTempView(inputDf, "null_err_detector_input", cache = true) { inputView =>
      // Detects error erroneous cells in a given table
      val sqls = inputDf.columns.filter { c => c != rowId && targetAttrs.contains(c) }.map { attr =>
        s"""
           |SELECT `$rowId`, '$attr' AS attribute
           |FROM $inputView
           |WHERE `$attr` IS NULL
         """.stripMargin
      }

      if (sqls.isEmpty) {
        createEmptyResultDfFrom(inputDf, rowId)
      } else {
        val errCellDf = spark.sql(sqls.mkString(" UNION ALL "))
        loggingErrorStats("NULL-based error detector", qualifiedName, errCellDf)
        errCellDf
      }
    }
  }
}

object RegExErrorDetector extends ErrorDetector {

  override def detect(
      qualifiedName: String,
      rowId: String,
      targetAttrs: Seq[String],
      options: Map[String, Any] = Map.empty): DataFrame = {

    val inputDf = spark.table(qualifiedName)

    val targetColumn = getOptionValue[String]("attr", options)
    val regex = getOptionValue[String]("regex", options)
    if (!targetAttrs.contains(targetColumn) || regex == null || regex.trim.isEmpty) {
      createEmptyResultDfFrom(inputDf, rowId)
    } else {
      withTempView(inputDf, "regex_err_detector_input") { inputView =>
        val errCellDf = spark.sql(
          s"""
             |SELECT `$rowId`, '$targetColumn' AS attribute
             |FROM $inputView
             |WHERE CAST(`$targetColumn` AS STRING) NOT RLIKE '$regex' OR `$targetColumn` IS NULL
           """.stripMargin)

        loggingErrorStats("RegEx-based error detector", qualifiedName, errCellDf)
        errCellDf
      }
    }
  }
}

object ConstraintErrorDetector extends ErrorDetector {

  override def detect(
      qualifiedName: String,
      rowId: String,
      targetAttrs: Seq[String],
      options: Map[String, Any] = Map.empty): DataFrame = {
    val inputDf = spark.table(qualifiedName)
    val constraintStmts = {
      val path = getOptionValue[String]("constraintFilePath", options)
      val constraintString = getOptionValue[String]("constraints", options)
      DenialConstraints.loadConstraintStmtsFromFile(path) ++
        DenialConstraints.loadConstraintStmtsFromString(constraintString)
    }
    if (constraintStmts.isEmpty) {
      createEmptyResultDfFrom(inputDf, rowId)
    } else {
      withTempView(inputDf, "constraint_err_detector_input", cache = true) { inputView =>
        val constraints = DenialConstraints.parseAndVerifyConstraints(
          constraintStmts, qualifiedName, inputDf.columns.toSeq)
        if (constraints.predicates.isEmpty) {
          createEmptyResultDfFrom(inputDf, rowId)
        } else {
          // Detects error erroneous cells in a given table
          val sqls = constraints.predicates.flatMap { preds =>
            import DenialConstraints._
            val attrs = preds.flatMap(_.references).filter(targetAttrs.contains).distinct
            if (attrs.nonEmpty) {
              // TODO: Needs to look for a more smart logic to filter error cells
              Some(s"""
                 |SELECT DISTINCT `$rowId`, explode(array(${attrs.map(a => s"'$a'").mkString(",")})) attribute
                 |FROM (
                 |  SELECT $leftRelationIdent.`$rowId` FROM $inputView AS $leftRelationIdent
                 |  WHERE EXISTS (
                 |    SELECT `$rowId` FROM $inputView AS $rightRelationIdent
                 |    WHERE ${preds.mkString(" AND ")}
                 |  )
                 |)
               """.stripMargin)
            } else {
              None
            }
          }

          if (sqls.isEmpty) {
            createEmptyResultDfFrom(inputDf, rowId)
          } else {
            val errCellDf = spark.sql(sqls.mkString(" UNION ALL "))
            loggingErrorStats("Constraint-based error detector", qualifiedName, errCellDf)
            errCellDf
          }
        }
      }
    }
  }
}

// TODO: Needs to support more sophisticated outlier detectors, e.g., a nonparametric histogram
// approach and a correlation based approach (named 'OD' in the HoloDetect paper [1]).
// We might be able to compute outliers by reusing [[RepairApi.computeDomainInErrorCells]].
object GaussianOutlierErrorDetector extends ErrorDetector {

  override def detect(
      qualifiedName: String,
      rowId: String,
      targetAttrs: Seq[String],
      options: Map[String, Any] = Map.empty): DataFrame = {

    val inputDf = spark.table(qualifiedName)
    val continousAttrs = {
      val attrs = SparkUtils.stringToSeq(getOptionValue[String]("continousAttrs", options))
      attrs.filter(targetAttrs.contains)
    }

    if (continousAttrs.isEmpty) {
      createEmptyResultDfFrom(inputDf, rowId)
    } else {
      val percentileExprs = continousAttrs.map { attr =>
        val approxEnabled = getOptionValue[Boolean]("approxEnabled", options)
        val expr = if (approxEnabled) {
          s"percentile_approx($attr, array(0.25, 0.75), 1000)"
        } else {
          s"percentile($attr, array(0.25, 0.75))"
        }
        s"CAST($expr AS ARRAY<DOUBLE>) $attr"
      }

      val percentileRow = spark.sql(
        s"""
           |SELECT ${percentileExprs.mkString(", ")}
           |FROM $qualifiedName
         """.stripMargin).collect.head

      val sqls = continousAttrs.zipWithIndex.map { case (attr, i) =>
        // Detects outliers simply based on a Box-and-Whisker plot
        // TODO: Needs to support more sophisticated ways to detect outliers
        val Seq(q1, q3) = percentileRow.getSeq[Double](i)
        val (lower, upper) = (q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1))
        logBasedOnLevel(s"Non-outlier values in $attr should be in [$lower, $upper]")
        s"""
           |SELECT `$rowId`, '$attr' attribute
           |FROM $qualifiedName
           |WHERE `$attr` < $lower OR `$attr` > $upper
         """.stripMargin
      }

      val errCellDf = spark.sql(sqls.mkString(" UNION ALL "))
      loggingErrorStats("Outlier-based error detector", qualifiedName, errCellDf)
      errCellDf
    }
  }
}
