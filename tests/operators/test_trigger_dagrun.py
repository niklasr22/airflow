#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import tempfile
from datetime import datetime
from unittest import mock

import pytest
import time_machine

from airflow.configuration import conf
from airflow.exceptions import AirflowException, DagRunAlreadyExists, TaskDeferred
from airflow.models.dag import DagModel
from airflow.models.dagrun import DagRun
from airflow.models.log import Log
from airflow.models.taskinstance import TaskInstance
from airflow.providers.standard.operators.trigger_dagrun import TriggerDagRunOperator
from airflow.providers.standard.triggers.external_task import DagStateTrigger
from airflow.sdk.execution_time.comms import XComResult
from airflow.utils import timezone
from airflow.utils.session import create_session
from airflow.utils.state import DagRunState, State, TaskInstanceState
from airflow.utils.types import DagRunType

from tests_common.test_utils.db import parse_and_sync_to_db
from tests_common.test_utils.version_compat import AIRFLOW_V_3_0_PLUS

pytestmark = pytest.mark.db_test

DEFAULT_DATE = datetime(2019, 1, 1, tzinfo=timezone.utc)
DEFAULT_RUN_ID = "testing_run_id"
TEST_DAG_ID = "testdag"
TRIGGERED_DAG_ID = "triggerdag"
DAG_SCRIPT = f"""\
from datetime import datetime
from airflow.models import DAG
from airflow.providers.standard.operators.empty import EmptyOperator

dag = DAG(
    dag_id='{TRIGGERED_DAG_ID}',
    schedule=None,
    start_date=datetime(2019, 1, 1),
)

task = EmptyOperator(task_id='test', dag=dag)
"""


class TestDagRunOperator:
    def setup_method(self):
        # Airflow relies on reading the DAG from disk when triggering it.
        # Therefore write a temp file holding the DAG to trigger.
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as f:
            self._tmpfile = f.name
            f.write(DAG_SCRIPT)
            f.flush()
        self.f_name = f.name

        with create_session() as session:
            session.add(DagModel(dag_id=TRIGGERED_DAG_ID, fileloc=self._tmpfile))
            session.commit()

    def teardown_method(self):
        """Cleanup state after testing in DB."""
        with create_session() as session:
            session.query(Log).filter(Log.dag_id == TEST_DAG_ID).delete(synchronize_session=False)
            for dbmodel in [DagModel, DagRun, TaskInstance]:
                session.query(dbmodel).filter(dbmodel.dag_id.in_([TRIGGERED_DAG_ID, TEST_DAG_ID])).delete(
                    synchronize_session=False
                )

        # pathlib.Path(self._tmpfile).unlink()

    def assert_extra_link(self, triggered_dag_run, triggering_task, session):
        """Asserts whether the correct extra links url will be created."""
        triggering_ti = (
            session.query(TaskInstance)
            .filter_by(
                task_id=triggering_task.task_id,
                dag_id=triggering_task.dag_id,
            )
            .one()
        )

        if AIRFLOW_V_3_0_PLUS:
            base_url = conf.get_mandatory_value("api", "base_url").lower()
            expected_url = f"{base_url}dags/{triggered_dag_run.dag_id}/runs/{triggered_dag_run.run_id}"

            link = triggering_task.operator_extra_links[0].get_link(
                operator=triggering_task, ti_key=triggering_ti.key
            )

            assert link == expected_url, f"Expected {expected_url}, but got {link}"
        else:
            with mock.patch(
                "airflow.providers.standard.operators.trigger_dagrun.build_airflow_url_with_query"
            ) as mock_build_url:
                # This is equivalent of a task run calling this and pushing to xcom
                triggering_task.operator_extra_links[0].get_link(
                    operator=triggering_task, ti_key=triggering_ti.key
                )
                assert mock_build_url.called
            args, _ = mock_build_url.call_args
            expected_args = {
                "dag_id": triggered_dag_run.dag_id,
                "dag_run_id": triggered_dag_run.run_id,
            }
            assert expected_args in args

    def test_trigger_dagrun(self, dag_maker, mock_supervisor_comms):
        """Test TriggerDagRunOperator."""
        with time_machine.travel("2025-02-18T08:04:46Z", tick=False):
            with dag_maker(
                TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
            ):
                task = TriggerDagRunOperator(task_id="test_task", trigger_dag_id=TRIGGERED_DAG_ID)
            dag_maker.sync_dagbag_to_db()
            parse_and_sync_to_db(self.f_name)
            dag_maker.create_dagrun()
            task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

            dagrun = dag_maker.session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).one()
            assert dagrun.run_type == DagRunType.MANUAL
            actual_run_id = dagrun.run_id.rsplit("_", 1)[0]

            expected_run_id = DagRun.generate_run_id(
                run_type=DagRunType.MANUAL, run_after=timezone.utcnow()
            ).rsplit("_", 1)[0]

            assert actual_run_id == expected_run_id

            mock_supervisor_comms.get_message.return_value = XComResult(key="xcom_key", value=dagrun.run_id)

            self.assert_extra_link(dagrun, task, dag_maker.session)

    def test_trigger_dagrun_custom_run_id(self, dag_maker):
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_task",
                trigger_dag_id=TRIGGERED_DAG_ID,
                trigger_run_id="custom_run_id",
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()
        task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1
            assert dagruns[0].run_id == "custom_run_id"

    def test_trigger_dagrun_with_logical_date(self, dag_maker, mock_supervisor_comms):
        """Test TriggerDagRunOperator with custom logical_date."""
        custom_logical_date = timezone.datetime(2021, 1, 2, 3, 4, 5)
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_trigger_dagrun_with_logical_date",
                trigger_dag_id=TRIGGERED_DAG_ID,
                logical_date=custom_logical_date,
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()
        task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

        with create_session() as session:
            dagrun = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).one()
            assert dagrun.run_type == DagRunType.MANUAL
            assert dagrun.logical_date == custom_logical_date
            assert dagrun.run_id == DagRun.generate_run_id(
                run_type=DagRunType.MANUAL, logical_date=custom_logical_date, run_after=custom_logical_date
            )
            mock_supervisor_comms.get_message.return_value = XComResult(key="xcom_key", value=dagrun.run_id)
            self.assert_extra_link(dagrun, task, session)

    def test_trigger_dagrun_twice(self, dag_maker, mock_supervisor_comms):
        """Test TriggerDagRunOperator with custom logical_date."""
        utc_now = timezone.utcnow()
        run_id = f"manual__{utc_now.isoformat()}"
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_trigger_dagrun_with_logical_date",
                trigger_dag_id=TRIGGERED_DAG_ID,
                trigger_run_id=run_id,
                logical_date=utc_now,
                poke_interval=1,
                reset_dag_run=True,
                wait_for_completion=True,
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()
        dag_run = DagRun(
            dag_id=TRIGGERED_DAG_ID,
            logical_date=utc_now,
            data_interval=(utc_now, utc_now),
            run_after=utc_now,
            state=State.SUCCESS,
            run_type="manual",
            run_id=run_id,
        )
        dag_maker.session.add(dag_run)
        dag_maker.session.commit()
        task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

        dagruns = dag_maker.session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
        assert len(dagruns) == 1
        triggered_dag_run = dagruns[0]
        assert triggered_dag_run.run_type == DagRunType.MANUAL
        assert triggered_dag_run.logical_date == utc_now
        mock_supervisor_comms.get_message.return_value = XComResult(
            key="xcom_key", value=triggered_dag_run.run_id
        )
        self.assert_extra_link(triggered_dag_run, task, dag_maker.session)

    def test_trigger_dagrun_with_scheduled_dag_run(self, dag_maker, mock_supervisor_comms):
        """Test TriggerDagRunOperator with custom logical_date and scheduled dag_run."""
        utc_now = timezone.utcnow()
        run_id = f"scheduled__{utc_now.isoformat()}"
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_trigger_dagrun_with_logical_date",
                trigger_dag_id=TRIGGERED_DAG_ID,
                trigger_run_id=run_id,
                logical_date=utc_now,
                poke_interval=1,
                reset_dag_run=True,
                wait_for_completion=True,
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()
        run_id = f"scheduled__{utc_now.isoformat()}"
        dag_run = DagRun(
            dag_id=TRIGGERED_DAG_ID,
            logical_date=utc_now,
            data_interval=(utc_now, utc_now),
            run_after=utc_now,
            state=State.SUCCESS,
            run_type="scheduled",
            run_id=run_id,
        )
        dag_maker.session.add(dag_run)
        dag_maker.session.commit()
        task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

        dagruns = dag_maker.session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
        assert len(dagruns) == 1
        triggered_dag_run = dagruns[0]
        assert triggered_dag_run.logical_date == utc_now
        mock_supervisor_comms.get_message.return_value = XComResult(
            key="xcom_key", value=triggered_dag_run.run_id
        )
        self.assert_extra_link(triggered_dag_run, task, dag_maker.session)

    def test_trigger_dagrun_with_templated_logical_date(self, dag_maker, mock_supervisor_comms):
        """Test TriggerDagRunOperator with templated logical_date."""
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_trigger_dagrun_with_str_logical_date",
                trigger_dag_id=TRIGGERED_DAG_ID,
                logical_date="{{ logical_date }}",
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()
        task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1
            triggered_dag_run = dagruns[0]
            assert triggered_dag_run.run_type == DagRunType.MANUAL
            assert triggered_dag_run.logical_date == DEFAULT_DATE
            mock_supervisor_comms.get_message.return_value = XComResult(
                key="xcom_key", value=triggered_dag_run.run_id
            )
            self.assert_extra_link(triggered_dag_run, task, session)

    def test_trigger_dagrun_with_templated_trigger_dag_id(self, dag_maker, mock_supervisor_comms):
        """Test TriggerDagRunOperator with templated trigger dag id."""
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="__".join(["test_trigger_dagrun_with_templated_trigger_dag_id", TRIGGERED_DAG_ID]),
                trigger_dag_id="{{ ti.task_id.rsplit('.', 1)[-1].split('__')[-1] }}",
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()
        task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1
            triggered_dag_run = dagruns[0]
            assert triggered_dag_run.run_type == DagRunType.MANUAL
            assert triggered_dag_run.dag_id == TRIGGERED_DAG_ID
            mock_supervisor_comms.get_message.return_value = XComResult(
                key="xcom_key", value=triggered_dag_run.run_id
            )
            self.assert_extra_link(triggered_dag_run, task, session)

    def test_trigger_dagrun_operator_conf(self, dag_maker):
        """Test passing conf to the triggered DagRun."""
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_trigger_dagrun_with_str_logical_date",
                trigger_dag_id=TRIGGERED_DAG_ID,
                conf={"foo": "bar"},
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()
        task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1
            assert dagruns[0].conf == {"foo": "bar"}

    def test_trigger_dagrun_operator_templated_invalid_conf(self, dag_maker):
        """Test passing a conf that is not JSON Serializable raise error."""
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_trigger_dagrun_with_invalid_conf",
                trigger_dag_id=TRIGGERED_DAG_ID,
                conf={"foo": "{{ dag.dag_id }}", "datetime": timezone.utcnow()},
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()
        with pytest.raises(AirflowException, match="^conf parameter should be JSON Serializable$"):
            task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE)

    def test_trigger_dagrun_operator_templated_conf(self, dag_maker):
        """Test passing a templated conf to the triggered DagRun."""
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_trigger_dagrun_with_str_logical_date",
                trigger_dag_id=TRIGGERED_DAG_ID,
                conf={"foo": "{{ dag.dag_id }}"},
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()
        task.run(start_date=DEFAULT_DATE, end_date=DEFAULT_DATE, ignore_ti_state=True)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1
            assert dagruns[0].conf == {"foo": TEST_DAG_ID}

    def test_trigger_dagrun_with_reset_dag_run_false(self, dag_maker):
        """Test TriggerDagRunOperator without reset_dag_run."""
        logical_date = DEFAULT_DATE
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_task",
                trigger_dag_id=TRIGGERED_DAG_ID,
                trigger_run_id=None,
                logical_date=None,
                reset_dag_run=False,
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()
        task.run(start_date=logical_date, end_date=logical_date, ignore_ti_state=True)
        task.run(start_date=logical_date, end_date=logical_date, ignore_ti_state=True)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 2

    @pytest.mark.parametrize(
        "trigger_run_id, trigger_logical_date",
        [
            (None, DEFAULT_DATE),
            ("dummy_run_id", None),
            ("dummy_run_id", DEFAULT_DATE),
        ],
    )
    def test_trigger_dagrun_with_reset_dag_run_false_fail(
        self, trigger_run_id, trigger_logical_date, dag_maker
    ):
        """Test TriggerDagRunOperator without reset_dag_run but triggered dag fails."""
        logical_date = DEFAULT_DATE
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_task",
                trigger_dag_id=TRIGGERED_DAG_ID,
                trigger_run_id=trigger_run_id,
                logical_date=trigger_logical_date,
                reset_dag_run=False,
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()
        task.run(start_date=logical_date, end_date=logical_date, ignore_ti_state=True)

        with pytest.raises(DagRunAlreadyExists):
            task.run(start_date=logical_date, end_date=logical_date, ignore_ti_state=True)

    def test_trigger_dagrun_with_skip_when_already_exists(self, dag_maker):
        """Test TriggerDagRunOperator with skip_when_already_exists."""
        logical_date = DEFAULT_DATE
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_task",
                trigger_dag_id=TRIGGERED_DAG_ID,
                trigger_run_id="dummy_run_id",
                reset_dag_run=False,
                skip_when_already_exists=True,
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dr: DagRun = dag_maker.create_dagrun()
        task.run(start_date=logical_date, end_date=logical_date, ignore_ti_state=True)
        assert dr.get_task_instance("test_task").state == TaskInstanceState.SUCCESS
        task.run(start_date=logical_date, end_date=logical_date, ignore_ti_state=True)
        assert dr.get_task_instance("test_task").state == TaskInstanceState.SKIPPED

    @pytest.mark.parametrize(
        "trigger_run_id, trigger_logical_date, expected_dagruns_count",
        [
            (None, DEFAULT_DATE, 1),
            (None, None, 2),
            ("dummy_run_id", DEFAULT_DATE, 1),
            ("dummy_run_id", None, 1),
        ],
    )
    def test_trigger_dagrun_with_reset_dag_run_true(
        self, trigger_run_id, trigger_logical_date, expected_dagruns_count, dag_maker
    ):
        """Test TriggerDagRunOperator with reset_dag_run."""
        logical_date = DEFAULT_DATE
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_task",
                trigger_dag_id=TRIGGERED_DAG_ID,
                trigger_run_id=trigger_run_id,
                logical_date=trigger_logical_date,
                reset_dag_run=True,
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()
        task.run(start_date=logical_date, end_date=logical_date, ignore_ti_state=True)
        task.run(start_date=logical_date, end_date=logical_date, ignore_ti_state=True)

        with create_session() as session:
            dag_runs = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dag_runs) == expected_dagruns_count
            assert dag_runs[0].run_type == DagRunType.MANUAL

    def test_trigger_dagrun_with_wait_for_completion_true(self, dag_maker):
        """Test TriggerDagRunOperator with wait_for_completion."""
        logical_date = DEFAULT_DATE
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_task",
                trigger_dag_id=TRIGGERED_DAG_ID,
                logical_date=logical_date,
                wait_for_completion=True,
                poke_interval=10,
                allowed_states=[State.QUEUED],
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()
        task.run(start_date=logical_date, end_date=logical_date)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1

    def test_trigger_dagrun_with_wait_for_completion_true_fail(self, dag_maker):
        """Test TriggerDagRunOperator with wait_for_completion but triggered dag fails."""
        logical_date = DEFAULT_DATE
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_task",
                trigger_dag_id=TRIGGERED_DAG_ID,
                logical_date=logical_date,
                wait_for_completion=True,
                poke_interval=10,
                failed_states=[State.QUEUED],
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()
        with pytest.raises(AirflowException):
            task.run(start_date=logical_date, end_date=logical_date)

    def test_trigger_dagrun_triggering_itself(self, dag_maker):
        """Test TriggerDagRunOperator that triggers itself"""
        logical_date = DEFAULT_DATE
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_task",
                trigger_dag_id=TEST_DAG_ID,
                logical_date=timezone.utcnow(),
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()
        task.run(start_date=logical_date, end_date=logical_date)

        dagruns = (
            dag_maker.session.query(DagRun)
            .filter(DagRun.dag_id == TEST_DAG_ID)
            .order_by(DagRun.logical_date)
            .all()
        )
        assert len(dagruns) == 2
        triggered_dag_run = dagruns[1]
        assert triggered_dag_run.state == State.QUEUED

    def test_trigger_dagrun_with_wait_for_completion_true_defer_false(self, dag_maker):
        """Test TriggerDagRunOperator with wait_for_completion."""
        logical_date = DEFAULT_DATE
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_task",
                trigger_dag_id=TRIGGERED_DAG_ID,
                logical_date=logical_date,
                wait_for_completion=True,
                poke_interval=10,
                allowed_states=[State.QUEUED],
                deferrable=False,
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()
        task.run(start_date=logical_date, end_date=logical_date)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1

    def test_trigger_dagrun_with_wait_for_completion_true_defer_true(self, dag_maker):
        """Test TriggerDagRunOperator with wait_for_completion."""
        logical_date = DEFAULT_DATE
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_task",
                trigger_dag_id=TRIGGERED_DAG_ID,
                logical_date=logical_date,
                wait_for_completion=True,
                poke_interval=10,
                allowed_states=[State.QUEUED],
                deferrable=True,
                trigger_run_id=DEFAULT_RUN_ID,
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()

        task.run(start_date=logical_date, end_date=logical_date)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1
        trigger = DagStateTrigger(
            dag_id="down_stream",
            run_ids=[DEFAULT_RUN_ID],
            poll_interval=20,
            states=["success", "failed"],
        )

        task.execute_complete(context={}, event=trigger.serialize())

    def test_trigger_dagrun_with_wait_for_completion_true_defer_true_failure(self, dag_maker):
        """Test TriggerDagRunOperator wait_for_completion dag run in non defined state."""
        logical_date = DEFAULT_DATE
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_task",
                trigger_dag_id=TRIGGERED_DAG_ID,
                logical_date=logical_date,
                wait_for_completion=True,
                poke_interval=10,
                allowed_states=[State.SUCCESS],
                deferrable=True,
                trigger_run_id=DEFAULT_RUN_ID,
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()

        task.run(start_date=logical_date, end_date=logical_date)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1

        trigger = DagStateTrigger(
            dag_id="down_stream",
            run_ids=[DEFAULT_RUN_ID],
            poll_interval=20,
            states=["success", "failed"],
        )
        with pytest.raises(AirflowException, match="which is not in"):
            task.execute_complete(
                context={},
                event=trigger.serialize(),
            )

    def test_trigger_dagrun_with_wait_for_completion_true_defer_true_failure_2(self, dag_maker):
        """Test TriggerDagRunOperator  wait_for_completion dag run in failed state."""
        logical_date = DEFAULT_DATE
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_task",
                trigger_dag_id=TRIGGERED_DAG_ID,
                logical_date=logical_date,
                wait_for_completion=True,
                poke_interval=10,
                allowed_states=[State.SUCCESS],
                failed_states=[State.QUEUED],
                deferrable=True,
                trigger_run_id=DEFAULT_RUN_ID,
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()

        task.run(start_date=logical_date, end_date=logical_date)

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1

        trigger = DagStateTrigger(
            dag_id="down_stream",
            run_ids=[DEFAULT_RUN_ID],
            poll_interval=20,
            states=["success", "failed"],
        )

        with pytest.raises(AirflowException, match="failed with failed state"):
            task.execute_complete(context={}, event=trigger.serialize())

    @pytest.mark.parametrize(
        argnames=["trigger_logical_date"],
        argvalues=[
            pytest.param(DEFAULT_DATE, id=f"logical_date={DEFAULT_DATE}"),
            pytest.param(None, id="logical_date=None"),
        ],
    )
    def test_dagstatetrigger_run_id(self, trigger_logical_date, dag_maker):
        """Ensure that the DagStateTrigger is called with the triggered DAG's run id."""
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_task",
                trigger_dag_id=TRIGGERED_DAG_ID,
                logical_date=trigger_logical_date,
                wait_for_completion=True,
                poke_interval=5,
                allowed_states=[DagRunState.QUEUED],
                deferrable=True,
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()

        mock_task_defer = mock.MagicMock(side_effect=task.defer)
        with mock.patch.object(TriggerDagRunOperator, "defer", mock_task_defer), pytest.raises(TaskDeferred):
            task.execute({"task_instance": mock.MagicMock()})

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1

        assert mock_task_defer.call_args_list[0].kwargs["trigger"].run_ids == [dagruns[0].run_id]

    def test_dagstatetrigger_run_id_with_clear_and_reset(self, dag_maker):
        """Check DagStateTrigger is called with the triggered DAG's run_id on subsequent defers."""
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_task",
                trigger_dag_id=TRIGGERED_DAG_ID,
                trigger_run_id="custom_run_id",
                wait_for_completion=True,
                poke_interval=5,
                allowed_states=[DagRunState.QUEUED],
                deferrable=True,
                reset_dag_run=True,
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()

        mock_task_defer = mock.MagicMock(side_effect=task.defer)
        with mock.patch.object(TriggerDagRunOperator, "defer", mock_task_defer), pytest.raises(TaskDeferred):
            task.execute({"task_instance": mock.MagicMock()})

        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            run_id = dagruns[0].run_id
            assert len(dagruns) == 1

        assert mock_task_defer.call_args_list[0].kwargs["trigger"].run_ids == [run_id]

        # Simulate the TriggerDagRunOperator task being cleared (aka executed again). A DagRunAlreadyExists
        # exception should be raised because of the previous DAG run.
        with (
            mock.patch.object(TriggerDagRunOperator, "defer", mock_task_defer),
            pytest.raises((DagRunAlreadyExists, TaskDeferred)),
        ):
            task.execute({"task_instance": mock.MagicMock()})

        # Still only one DAG run should exist for the triggered DAG since the DAG will be cleared since the
        # TriggerDagRunOperator task is configured with `reset_dag_run=True`.
        with create_session() as session:
            dagruns = session.query(DagRun).filter(DagRun.dag_id == TRIGGERED_DAG_ID).all()
            assert len(dagruns) == 1

        # The second DagStateTrigger call should still use the original `logical_date` value.
        assert mock_task_defer.call_args_list[1].kwargs["trigger"].run_ids == [run_id]

    def test_trigger_dagrun_with_no_failed_state(self, dag_maker):
        logical_date = DEFAULT_DATE
        with dag_maker(
            TEST_DAG_ID, default_args={"owner": "airflow", "start_date": DEFAULT_DATE}, serialized=True
        ):
            task = TriggerDagRunOperator(
                task_id="test_task",
                trigger_dag_id=TRIGGERED_DAG_ID,
                logical_date=logical_date,
                wait_for_completion=True,
                poke_interval=10,
                failed_states=[],
            )
        dag_maker.sync_dagbag_to_db()
        parse_and_sync_to_db(self.f_name)
        dag_maker.create_dagrun()

        assert task.failed_states == []
