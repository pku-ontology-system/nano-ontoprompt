import DataSourcesPage from '@/pages/sources/DataSourcesPage';

/** The data-source page, inside an ontology's tabs: the same page, so data is brought in one way wherever it is started. */
export default function SourcesTab() {
  return <section aria-label="数据源"><DataSourcesPage embedded /></section>;
}
